"""Bumble's virtual controller, patched to behave like a real BR/EDR one,
and the H4-over-TCP server that gives one to the stack under test (the
simulator's HCI transport, $ROCKBOX_BT_HCI).

Bumble's controller is made for Bumble's own host. A real stack expects
more: Command Status for commands it does not support, encryption,
authentication that asks the host for its link key, page and accept
timeouts, inquiry, controller ACL buffers that fill and free at air speed,
and ACL fragmentation. Each patch logs "[patch KEY]" the first time it fires
(logs.Tally); controller A, the stack's, also reports what a real
controller would not accept from its host as a violation, which fails the
run.

Every TCP connection gets a fresh controller A with the same address, so the
stack may power-cycle Bluetooth (reconnect) at any time; the peer sees a
link loss.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import struct
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bumble import core, hci, lmp
from bumble.controller import Connection, Controller, SspState
from bumble.core import PhysicalTransport
from bumble.link import LocalLink
from bumble.snoop import BtSnooper, Snooper
from bumble.transport.common import PacketParser

from .logs import Tally, flat, h4log, log
from .options import StackSettings

E = hci.HCI_ErrorCode
SLOT_S = 0.000625
_M = hci.LmpFeatureMask

# Patch 4: Bumble's default lmp_features say BR_EDR_NOT_SUPPORTED. Bumble's
# own host needs nothing here (it only reads interlaced-scan bits), but a
# real stack checks SSP/EDR/encryption, so advertise a typical BR/EDR 2.1+EDR
# controller with SSP. Page 1 (host SSP bit) follows Write_Simple_Pairing_Mode.
CLASSIC_FEATURES = (
    _M.LMP_3_SLOT_PACKETS | _M.LMP_5_SLOT_PACKETS | _M.ENCRYPTION | _M.SLOT_OFFSET
    | _M.TIMING_ACCURACY | _M.ROLE_SWITCH | _M.SNIFF_MODE | _M.POWER_CONTROL_REQUESTS
    | _M.CHANNEL_QUALITY_DRIVEN_DATA_RATE_CQDDR | _M.PAGING_PARAMETER_NEGOTIATION
    | _M.POWER_CONTROL | _M.ENHANCED_DATA_RATE_ACL_2_MBPS_MODE
    | _M.ENHANCED_DATA_RATE_ACL_3_MBPS_MODE | _M.AFH_CAPABLE_PERIPHERAL
    | _M.AFH_CLASSIFICATION_PERIPHERAL | _M.AFH_CAPABLE_CENTRAL
    | _M.AFH_CLASSIFICATION_CENTRAL | _M.LMP_3_SLOT_ENHANCED_DATA_RATE_ACL_PACKETS
    | _M.LMP_5_SLOT_ENHANCED_DATA_RATE_ACL_PACKETS | _M.SNIFF_SUBRATING
    | _M.PAUSE_ENCRYPTION | _M.EXTENDED_INQUIRY_RESPONSE | _M.RSSI_WITH_INQUIRY_RESULTS
    | _M.SECURE_SIMPLE_PAIRING_CONTROLLER_SUPPORT | _M.ENCAPSULATED_PDU
    | _M.NON_FLUSHABLE_PACKET_BOUNDARY_FLAG
    | _M.HCI_LINK_SUPERVISION_TIMEOUT_CHANGED_EVENT | _M.EXTENDED_FEATURES)

# Configuration commands every real controller accepts but Bumble's does not
# implement (it would answer UNKNOWN_HCI_COMMAND): accepted and ignored, but
# for the page and accept timeouts.
ACCEPTED_SYNC_COMMANDS: tuple[type[hci.HCI_SyncCommand], ...] = (
    hci.HCI_Write_Page_Timeout_Command, hci.HCI_Write_Connection_Accept_Timeout_Command,
    hci.HCI_Write_Page_Scan_Activity_Command, hci.HCI_Write_Inquiry_Scan_Activity_Command,
    hci.HCI_Write_Page_Scan_Type_Command, hci.HCI_Write_Inquiry_Scan_Type_Command,
    hci.HCI_Write_Default_Link_Policy_Settings_Command,
    hci.HCI_Write_Link_Policy_Settings_Command, hci.HCI_Write_Link_Supervision_Timeout_Command,
    hci.HCI_Set_Event_Filter_Command, hci.HCI_Delete_Stored_Link_Key_Command,
    hci.HCI_Write_Authentication_Enable_Command, hci.HCI_Write_Voice_Setting_Command)

# Bumble's sync command classes come out of a class decorator that type
# checkers read as returning the base class, which hides their fields: their
# handlers take them as SyncCommand
type SyncCommand = Any

INQUIRY_RESULTS_S = 0.5
INQUIRY_MAX_S = 2.0
INQUIRY_RSSI = -50


@dataclass(eq=False)
class LinkState:
    """What the patches keep about one of Bumble's link connections"""

    conn: Connection
    dead: bool = False              # dropped: queued ACL data is flushed, not completed
    encrypted: bool = False
    in_flight: int = 0              # host ACL packets not yet "sent"
    pdu_left: int = 0               # bytes still due for the L2CAP PDU being sent
    accept_timer: asyncio.TimerHandle | None = None
    auth_role: str | None = None    # "verifier" or "claimant" of a stored-key check
    auth_key: bytes | None = None   # the verifier's key, on the claimant's side
    auth_verifier: tuple[PeerController, Connection] | None = None


class PeerController(Controller):
    lmp_features = CLASSIC_FEATURES | _M.LE_SUPPORTED_CONTROLLER

    def __init__(self, name: str, link: LocalLink, address: str, tally: Tally, *,
                 acl_size: int, acl_count: int, air_kbps: int = 0, le: bool = True,
                 checks: bool = False, eir: bool = True) -> None:
        super().__init__(name, link=link, public_address=address)
        self.tally = tally
        if not le:
            self.lmp_features = CLASSIC_FEATURES
        self.eir = eir
        if not eir:
            self.lmp_features &= ~_M.EXTENDED_INQUIRY_RESPONSE
        self.inquiry_mode = 0
        self.inquiry: list[asyncio.TimerHandle] = []
        # Patch 5a: Read_Buffer_Size values (Bumble's default is 27 x 64)
        self.acl_data_packet_length = acl_size
        self.total_num_acl_data_packets = acl_count
        self.air_kbps = air_kbps        # 0: complete host packets at once
        self.checks = checks            # report host protocol violations
        self.class_of_device = 0
        self.page_timeout = 0x2000 * SLOT_S
        self.accept_timeout = 0x1FA0 * SLOT_S
        self.timers: set[asyncio.TimerHandle] = set()
        self.air_free_at = 0.0
        self.air_queue: deque[tuple[LinkState, hci.HCI_AclDataPacket]] = deque()
        self.links: dict[int, LinkState] = {}
        # Read_Local_Supported_Commands lists exactly what has a handler
        # (Bumble's fixed list omits all the classic commands it implements)
        self.supported_commands = {
            op for op in hci.HCI_SUPPORTED_COMMANDS_MASKS
            if hasattr(self, "on_" + hci.HCI_Command.command_name(op).lower())
        } | {c.op_code for c in ACCEPTED_SYNC_COMMANDS}

    # -- helpers -------------------------------------------------------------
    def state(self, conn: Connection) -> LinkState:
        """CONN's patch state (made on first use; states of links that are
        gone are dropped then)"""
        st = self.links.get(id(conn))
        if st is None or st.conn is not conn:
            live = {id(c) for c in self.classic_connections.values()}
            self.links = {k: v for k, v in self.links.items() if k in live}
            st = self.links[id(conn)] = LinkState(conn)
        return st

    def _later(self, delay: float, fn: Callable[[], object]) -> asyncio.TimerHandle:
        def run() -> None:
            self.timers.discard(handle)
            fn()
        handle = asyncio.get_running_loop().call_later(max(0.0, delay), run)
        self.timers.add(handle)
        return handle

    def _peer(self, address: hci.Address) -> tuple[PeerController | None, Connection | None]:
        peer = self.link.find_classic_controller(address)
        if not isinstance(peer, PeerController):
            return None, None
        return peer, peer.classic_connections.get(self.public_address)

    def _lmp(self, address: hci.Address, packet: lmp.Packet) -> None:
        with contextlib.suppress(core.InvalidArgumentError):    # the peer has gone
            self.send_lmp_packet(address, packet)

    def _connection_failed(self, address: hci.Address, status: int) -> None:
        self.send_hci_packet(hci.HCI_Connection_Complete_Event(
            status=status, connection_handle=0, bd_addr=address,
            link_type=hci.HCI_Connection_Complete_Event.LinkType.ACL,
            encryption_enabled=0))

    def _drop_links(self, reason: int) -> None:
        """Links vanish without LMP_detach: the peers see a link loss"""
        for handle in list(self.timers):
            handle.cancel()
        self.timers.clear()
        self.air_queue.clear()
        for address, conn in list(self.classic_connections.items()):
            self.state(conn).dead = True
            peer, pconn = self._peer(address)
            if peer is None or pconn is None:
                continue
            if pconn.handle:
                peer.on_classic_disconnected(self.public_address, reason)
                continue
            pending = peer.classic_pending_commands.get(self.public_address, {})
            future = pending.pop(lmp.Opcode.LMP_HOST_CONNECTION_REQ, None)
            if future and not future.done():
                future.set_result(E.PAGE_TIMEOUT_ERROR)
            else:
                peer.classic_connections.pop(self.public_address, None)
        self.classic_connections.clear()

    def lose_links(self) -> None:
        """A link loss (out of range): both hosts see Connection Timeout"""
        handles = [conn.handle for conn in self.classic_connections.values()]
        self._drop_links(E.CONNECTION_TIMEOUT_ERROR)
        for handle in handles:
            self.send_hci_packet(hci.HCI_Disconnection_Complete_Event(
                status=E.SUCCESS, connection_handle=handle,
                reason=E.CONNECTION_TIMEOUT_ERROR))

    def shutdown(self) -> None:
        """The HCI transport closed: this controller leaves the link"""
        self.host = None
        self._drop_links(E.CONNECTION_TIMEOUT_ERROR)
        if self in self.link.controllers:
            self.link.remove_controller(self)

    # -- patch 1: unsupported commands get an answer --------------------------
    def on_hci_command_packet(self, command: hci.HCI_Command) -> None:
        if isinstance(command, ACCEPTED_SYNC_COMMANDS):
            self.send_hci_packet(hci.HCI_Command_Complete_Event(
                num_hci_command_packets=1, command_opcode=command.op_code,
                return_parameters=self._accept_and_ignore(command)))
            return
        if not hasattr(self, f"on_{command.name.lower()}"):
            if isinstance(command, hci.HCI_AsyncCommand):
                self.tally.patch("1", f"unsupported async {command.name} -> Command Status "
                                      "UNKNOWN_HCI_COMMAND (Bumble sends no event at all)")
                self._send_hci_command_status(E.UNKNOWN_HCI_COMMAND_ERROR, command.op_code)
            else:
                if not isinstance(command, hci.HCI_SyncCommand):
                    self.tally.patch("1b", f"unknown opcode 0x{command.op_code:04X} -> "
                                           "Command Complete UNKNOWN_HCI_COMMAND")
                self.send_hci_packet(hci.HCI_Command_Complete_Event(
                    num_hci_command_packets=1, command_opcode=command.op_code,
                    return_parameters=hci.HCI_StatusReturnParameters(
                        E.UNKNOWN_HCI_COMMAND_ERROR)))
            if self.checks:
                log.warning("controller %s: unsupported HCI command %s", self.name,
                            command.name)
            return
        try:
            super().on_hci_command_packet(command)
        except Exception:
            log.exception("controller %s: error handling %s", self.name, command.name)

    def _accept_and_ignore(self, command: SyncCommand) -> hci.HCI_ReturnParameters:
        self.tally.patch("cfg", f"{command.name} accepted (only page/accept timeouts "
                                "take effect)")
        if command.op_code == hci.HCI_Write_Page_Timeout_Command.op_code:
            self.page_timeout = command.page_timeout * SLOT_S
        elif command.op_code == hci.HCI_Write_Connection_Accept_Timeout_Command.op_code:
            self.accept_timeout = command.connection_accept_timeout * SLOT_S
        rp = command.return_parameters_class
        values = {f.name: getattr(command, f.name, 0) for f in dataclasses.fields(rp)}
        values["status"] = E.SUCCESS
        return rp(**values)

    # -- patch 2: classic encryption ----------------------------------------
    def on_hci_set_connection_encryption_command(
            self, command: hci.HCI_Set_Connection_Encryption_Command) -> None:
        conn = self.find_classic_connection_by_handle(command.connection_handle)
        if conn is None:
            self._send_hci_command_status(E.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
                                          command.op_code)
            return
        if conn.ssp_state.link_key is None:
            # A real controller needs a link key (authentication) first
            if self.checks:
                self.tally.violation("encryption", "Set_Connection_Encryption before "
                                     "authentication -> Command Status COMMAND_DISALLOWED")
            self._send_hci_command_status(E.COMMAND_DISALLOWED_ERROR, command.op_code)
            return
        self._send_hci_command_status(E.SUCCESS, command.op_code)
        enabled = 1 if command.encryption_enable else 0
        self.tally.patch("2", f"Set_Connection_Encryption({enabled}) -> Encryption_Change "
                              "to both hosts")
        self.state(conn).encrypted = bool(enabled)
        self.send_hci_packet(hci.HCI_Encryption_Change_Event(
            status=E.SUCCESS, connection_handle=conn.handle, encryption_enabled=enabled))
        peer, pconn = self._peer(conn.peer_address)
        if peer is not None and pconn is not None:
            peer.state(pconn).encrypted = bool(enabled)
            peer.send_hci_packet(hci.HCI_Encryption_Change_Event(
                status=E.SUCCESS, connection_handle=pconn.handle,
                encryption_enabled=enabled))

    def on_hci_read_encryption_key_size_command(
            self, command: SyncCommand
    ) -> hci.HCI_Read_Encryption_Key_Size_ReturnParameters:
        conn = self.find_classic_connection_by_handle(command.connection_handle)
        status = (E.UNKNOWN_CONNECTION_IDENTIFIER_ERROR if conn is None else
                  E.SUCCESS if self.state(conn).encrypted else E.COMMAND_DISALLOWED_ERROR)
        return hci.HCI_Read_Encryption_Key_Size_ReturnParameters(
            status=status, connection_handle=command.connection_handle, key_size=16)

    # -- patch 3: authentication asks the host for the link key first -------
    def on_hci_authentication_requested_command(
            self, command: hci.HCI_Authentication_Requested_Command) -> None:
        conn = self.find_classic_connection_by_handle(command.connection_handle)
        if conn is None:
            self._send_hci_command_status(E.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
                                          command.op_code)
            return
        self._send_hci_command_status(E.SUCCESS, command.op_code)
        conn.ssp_state = SspState(is_initiator=True)
        self.state(conn).auth_role = "verifier"
        self.tally.patch("3", "Authentication_Requested -> Link_Key_Request to the host "
                              "(Bumble jumps straight to the IO capability exchange)")
        self.send_hci_packet(hci.HCI_Link_Key_Request_Event(bd_addr=conn.peer_address))

    # Patch 3b: with a stored key, the verifier's key is checked against the
    # claimant: the peer host gets a Link_Key_Request too (as with LMP_au_rand),
    # and its reply decides Authentication_Complete (success, 0x05 key
    # mismatch, 0x06 key missing). Bumble just reports success.
    def on_hci_link_key_request_reply_command(
            self, command: SyncCommand
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        conn = self.classic_connections.get(command.bd_addr)
        st = self.state(conn) if conn is not None else None
        ok = hci.HCI_StatusAndAddressReturnParameters(status=E.SUCCESS,
                                                      bd_addr=command.bd_addr)
        if conn is not None and st is not None and st.auth_role == "verifier":
            st.auth_role = None
            conn.ssp_state.link_key = command.link_key
            peer, pconn = self._peer(conn.peer_address)
            if peer is None or pconn is None:
                return super().on_hci_link_key_request_reply_command(command)
            self.tally.patch("3b", "stored-key authentication: the peer host is asked for "
                                   "its link key and the two keys are compared")
            pst = peer.state(pconn)
            pst.auth_role = "claimant"
            pst.auth_key = command.link_key
            pst.auth_verifier = (self, conn)
            peer.send_hci_packet(hci.HCI_Link_Key_Request_Event(bd_addr=self.public_address))
            return ok
        if conn is not None and st is not None and st.auth_role == "claimant":
            st.auth_role = None
            assert st.auth_verifier is not None
            verifier, vconn = st.auth_verifier
            match = command.link_key == st.auth_key
            conn.ssp_state.link_key = command.link_key if match else None
            vconn.ssp_state.link_key = command.link_key if match else None
            vconn.ssp_state.authenticated = match
            log.info("security: %s authenticated %s with its stored link key: %s",
                     verifier.name, self.name, "keys match" if match else "KEYS DIFFER (0x05)")
            verifier.send_hci_packet(hci.HCI_Authentication_Complete_Event(
                status=E.SUCCESS if match else E.AUTHENTICATION_FAILURE_ERROR,
                connection_handle=vconn.handle))
            return ok
        return super().on_hci_link_key_request_reply_command(command)

    def on_hci_link_key_request_negative_reply_command(
            self, command: SyncCommand
    ) -> hci.HCI_StatusAndAddressReturnParameters:
        conn = self.classic_connections.get(command.bd_addr)
        st = self.state(conn) if conn is not None else None
        if st is not None and st.auth_role == "claimant":
            st.auth_role = None
            assert st.auth_verifier is not None
            verifier, vconn = st.auth_verifier
            vconn.ssp_state.link_key = None
            self.tally.patch("3e", "claimant has no link key -> verifier gets "
                                   "Authentication_Complete PIN_OR_KEY_MISSING")
            verifier.send_hci_packet(hci.HCI_Authentication_Complete_Event(
                status=E.PIN_OR_KEY_MISSING_ERROR, connection_handle=vconn.handle))
            return hci.HCI_StatusAndAddressReturnParameters(status=E.SUCCESS,
                                                            bd_addr=command.bd_addr)
        if conn is not None and st is not None:
            st.auth_role = None
            conn.ssp_state = SspState()     # fresh SSP state (Bumble keeps stale nonces)
        return super().on_hci_link_key_request_negative_reply_command(command)

    def on_lmp_packet(self, sender_address: hci.Address, packet: lmp.Packet) -> None:
        if isinstance(packet, lmp.LmpIoCapabilityReq) and (
                conn := self.classic_connections.get(sender_address)):
            conn.ssp_state = SspState()     # responder side of a new pairing
        super().on_lmp_packet(sender_address, packet)

    def send_hci_packet(self, packet: hci.HCI_Packet) -> None:
        if isinstance(packet, hci.HCI_Authentication_Complete_Event):
            conn = self.find_classic_connection_by_handle(packet.connection_handle)
            if conn is not None and not conn.ssp_state.is_initiator:
                self.tally.patch("3c", "Authentication_Complete not sent to the pairing "
                                       "responder (only the initiator gets it)")
                return
        elif isinstance(packet, hci.HCI_Link_Key_Notification_Event):
            conn = self.classic_connections.get(packet.bd_addr)
            s = conn.ssp_state if conn else None
            if s and hci.IoCapability.NO_INPUT_NO_OUTPUT in (s.local_io_capability,
                                                             s.peer_io_capability):
                self.tally.patch("3d", "Just Works link key type 0x04 (unauthenticated "
                                       "P-192; Bumble says authenticated P-256)")
                packet = hci.HCI_Link_Key_Notification_Event(
                    bd_addr=packet.bd_addr, link_key=packet.link_key,
                    key_type=hci.LinkKeyType.UNAUTHENTICATED_COMBINATION_KEY_GENERATED_FROM_P_192)
        super().send_hci_packet(packet)

    # -- connection setup: page timeout, accept timeout, reject, CoD --------
    def on_hci_create_connection_command(
            self, command: hci.HCI_Create_Connection_Command) -> None:
        existing = self.classic_connections.get(command.bd_addr)
        if existing is not None and existing.handle:
            self._send_hci_command_status(E.CONNECTION_ALREADY_EXISTS_ERROR, command.op_code)
            return
        peer = self.link.find_classic_controller(command.bd_addr)
        if peer is None or not peer.classic_scan_enable & 0x02:
            # Bumble raises (unknown address) or connects anyway (no page scan)
            self._send_hci_command_status(E.SUCCESS, command.op_code)
            self.tally.patch("page", f"page to {command.bd_addr}: no page-scanning device -> "
                                     f"Connection_Complete PAGE_TIMEOUT after "
                                     f"{self.page_timeout:.2f} s")
            self._later(self.page_timeout, lambda: self._connection_failed(
                command.bd_addr, E.PAGE_TIMEOUT_ERROR))
            return
        super().on_hci_create_connection_command(command)

    def on_classic_connection_request(self, peer_address: hci.Address, link_type: int) -> None:
        if link_type != hci.HCI_Connection_Complete_Event.LinkType.ACL:
            super().on_classic_connection_request(peer_address, link_type)
            return
        peer = self.link.find_classic_controller(peer_address)
        conn = Connection(
            controller=self, handle=0, role=hci.Role.PERIPHERAL,
            self_address=self.public_address, peer_address=peer_address,
            link=self.link, transport=PhysicalTransport.BR_EDR, link_type=link_type,
            classic_allow_role_switch=self.classic_allow_role_switch)
        self.classic_connections[peer_address] = conn
        self.state(conn).accept_timer = self._later(
            self.accept_timeout, lambda: self._accept_timed_out(peer_address, conn))
        # Bumble reports class_of_device 0; report the pager's CoD
        cod = peer.class_of_device if isinstance(peer, PeerController) else 0
        self.send_hci_packet(hci.HCI_Connection_Request_Event(
            bd_addr=peer_address, class_of_device=cod, link_type=link_type))

    def _accept_timed_out(self, peer_address: hci.Address, conn: Connection) -> None:
        if self.classic_connections.get(peer_address) is not conn or conn.handle:
            return
        del self.classic_connections[peer_address]
        self.tally.patch("accept", "host did not answer Connection_Request -> "
                                   "CONNECTION_ACCEPT_TIMEOUT on both sides")
        if self.checks:
            self.tally.violation("accept", f"no Accept/Reject_Connection_Request for "
                                 f"{peer_address} within {self.accept_timeout:.1f} s")
        self._lmp(peer_address, lmp.LmpNotAccepted(lmp.Opcode.LMP_HOST_CONNECTION_REQ,
                                                   E.CONNECTION_ACCEPT_TIMEOUT_ERROR))
        self._connection_failed(peer_address, E.CONNECTION_ACCEPT_TIMEOUT_ERROR)

    def _cancel_accept_timer(self, conn: Connection) -> None:
        st = self.state(conn)
        if st.accept_timer is not None:
            st.accept_timer.cancel()
            st.accept_timer = None

    def on_hci_accept_connection_request_command(
            self, command: hci.HCI_Accept_Connection_Request_Command) -> None:
        if conn := self.classic_connections.get(command.bd_addr):
            self._cancel_accept_timer(conn)
        super().on_hci_accept_connection_request_command(command)

    def on_hci_reject_connection_request_command(
            self, command: hci.HCI_Reject_Connection_Request_Command) -> None:
        conn = self.classic_connections.get(command.bd_addr)
        if conn is None or conn.handle:
            self._send_hci_command_status(E.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
                                          command.op_code)
            return
        self._send_hci_command_status(E.SUCCESS, command.op_code)
        self._cancel_accept_timer(conn)
        del self.classic_connections[command.bd_addr]
        self._lmp(command.bd_addr, lmp.LmpNotAccepted(lmp.Opcode.LMP_HOST_CONNECTION_REQ,
                                                      command.reason))
        self._connection_failed(command.bd_addr, command.reason)

    def on_classic_connection_complete(self, peer_address: hci.Address, status: int) -> None:
        super().on_classic_connection_complete(peer_address, status)
        if status != E.SUCCESS:
            self.classic_connections.pop(peer_address, None)   # Bumble leaves it

    def on_classic_disconnected(self, peer_address: hci.Address, reason: int) -> None:
        if conn := self.classic_connections.get(peer_address):
            self.state(conn).dead = True
        super().on_classic_disconnected(peer_address, reason)

    def on_hci_write_class_of_device_command(
            self, _command: SyncCommand
    ) -> hci.HCI_StatusReturnParameters:
        self.class_of_device = _command.class_of_device
        return hci.HCI_StatusReturnParameters(E.SUCCESS)

    def on_hci_read_class_of_device_command(
            self, _command: SyncCommand
    ) -> hci.HCI_Read_Class_Of_Device_ReturnParameters:
        return hci.HCI_Read_Class_Of_Device_ReturnParameters(
            status=E.SUCCESS, class_of_device=self.class_of_device)

    def on_hci_read_remote_version_information_command(
            self, command: hci.HCI_Read_Remote_Version_Information_Command) -> None:
        conn = self.find_classic_connection_by_handle(command.connection_handle)
        peer = self.link.find_classic_controller(conn.peer_address) if conn else None
        if conn is None or peer is None:
            self._send_hci_command_status(E.UNKNOWN_CONNECTION_IDENTIFIER_ERROR,
                                          command.op_code)
            return
        self._send_hci_command_status(E.SUCCESS, command.op_code)
        self.send_hci_packet(hci.HCI_Read_Remote_Version_Information_Complete_Event(
            status=E.SUCCESS, connection_handle=conn.handle, version=peer.lmp_version,
            manufacturer_name=peer.manufacturer_company_identifier,
            subversion=peer.lmp_subversion))

    def on_hci_reset_command(self, _command: SyncCommand) -> hci.HCI_StatusReturnParameters:
        # Bumble's Reset keeps all state; drop links, scans and SSP mode
        if self.classic_connections:
            self.tally.patch("reset", "HCI_Reset with open links: the peers see a link loss")
        self._drop_links(E.CONNECTION_TIMEOUT_ERROR)
        self.classic_scan_enable = 0
        self.inquiry_mode = 0
        self.lmp_features &= ~_M.SECURE_SIMPLE_PAIRING_HOST_SUPPORT
        return super().on_hci_reset_command(_command)

    # -- inquiry (Bumble has none): one result per discoverable peer ---------
    def on_hci_write_inquiry_mode_command(
            self, command: SyncCommand) -> hci.HCI_StatusReturnParameters:
        if command.inquiry_mode == 2 and not self.eir:
            return hci.HCI_StatusReturnParameters(E.UNSUPPORTED_FEATURE_OR_PARAMETER_VALUE_ERROR)
        self.inquiry_mode = command.inquiry_mode
        return hci.HCI_StatusReturnParameters(E.SUCCESS)

    def on_hci_inquiry_command(self, command: hci.HCI_Inquiry_Command) -> None:
        self._send_hci_command_status(E.SUCCESS, command.op_code)
        length = min(command.inquiry_length * 1.28, INQUIRY_MAX_S)
        self.tally.patch("inquiry", f"Inquiry (mode {self.inquiry_mode}): results for "
                                    f"inquiry-scanning devices after {INQUIRY_RESULTS_S} s, "
                                    f"Inquiry_Complete after {length:.2f} s")
        self.inquiry = [self._later(INQUIRY_RESULTS_S, self._inquiry_results),
                        self._later(length, self._inquiry_done)]

    def _inquiry_results(self) -> None:
        for peer in list(self.link.controllers):
            if peer is self or not peer.classic_scan_enable & 0x01:
                continue
            a = peer.public_address
            cod = peer.class_of_device if isinstance(peer, PeerController) else 0
            if self.inquiry_mode == 2:
                name = peer.local_name.encode()[:238]
                self.send_hci_packet(hci.HCI_Extended_Inquiry_Result_Event(
                    num_responses=1, bd_addr=a, page_scan_repetition_mode=1, reserved=0,
                    class_of_device=cod, clock_offset=0, rssi=INQUIRY_RSSI,
                    extended_inquiry_response=(bytes([len(name) + 1, 0x09]) + name)
                    .ljust(240, b"\0")))
            elif self.inquiry_mode == 1:
                self.send_hci_packet(hci.HCI_Inquiry_Result_With_RSSI_Event(
                    bd_addr=[a], page_scan_repetition_mode=[1], reserved=[0],
                    class_of_device=[cod], clock_offset=[0], rssi=[INQUIRY_RSSI]))
            else:
                self.send_hci_packet(hci.HCI_Inquiry_Result_Event(
                    bd_addr=[a], page_scan_repetition_mode=[1], reserved_0=[0],
                    reserved_1=[0], class_of_device=[cod], clock_offset=[0]))

    def _inquiry_done(self) -> None:
        self.inquiry = []
        self.send_hci_packet(hci.HCI_Inquiry_Complete_Event(status=E.SUCCESS))

    def on_hci_inquiry_cancel_command(
            self, _command: SyncCommand) -> hci.HCI_StatusReturnParameters:
        for handle in self.inquiry:
            handle.cancel()
        self.inquiry = []
        return hci.HCI_StatusReturnParameters(E.SUCCESS)

    # -- patch 5: ACL buffers, fragmentation and air time -------------------
    def on_link_acl_data(self, sender_address: hci.Address, transport: PhysicalTransport,
                         data: bytes) -> None:
        if transport != PhysicalTransport.BR_EDR:
            super().on_link_acl_data(sender_address, transport, data)
            return
        conn = self.classic_connections.get(sender_address)
        if conn is None:
            log.warning("controller %s: ACL from %s without a connection", self.name,
                        sender_address)
            return
        # Bumble sends whole L2CAP PDUs to its host ("TODO: should fragment")
        size = self.acl_data_packet_length
        if len(data) > size:
            self.tally.patch("5", f"{len(data)}-byte L2CAP PDU sent to host {self.name} as "
                                  f"{size}-byte ACL fragments (PB 0b10 then 0b01)")
        for offset in range(0, len(data), size):
            chunk = data[offset:offset + size]
            self.send_hci_packet(hci.HCI_AclDataPacket(
                conn.handle, 0b10 if offset == 0 else 0b01, 0, len(chunk), chunk))

    def on_hci_acl_data_packet(self, packet: hci.HCI_AclDataPacket) -> None:
        conn = self.find_classic_connection_by_handle(packet.connection_handle)
        if conn is None:
            if self.checks:
                self.tally.violation("acl", "ACL for unknown handle "
                                     f"0x{packet.connection_handle:03x}")
            super().on_hci_acl_data_packet(packet)
            return
        st = self.state(conn)
        if self.checks:
            self._check_acl(st, packet)
        st.in_flight += 1
        delay = 0.0
        if self.air_kbps:
            # Model air time: buffers free up (Number_Of_Completed_Packets) and
            # data reach the peer only as fast as the radio could send them
            now = asyncio.get_running_loop().time()
            self.air_free_at = (max(now, self.air_free_at)
                                + (len(packet.data) + 10) * 8 / (self.air_kbps * 1000))
            delay = self.air_free_at - now
        # Timers can fire out of order (a call_later preempted on a busy host
        # lands late), so each one sends the oldest packet: order stays FIFO
        self.air_queue.append((st, packet))
        self._later(delay, self._acl_sent)

    def _check_acl(self, st: LinkState, packet: hci.HCI_AclDataPacket) -> None:
        in_flight = sum(self.state(c).in_flight for c in self.classic_connections.values())
        if len(packet.data) > self.acl_data_packet_length:
            self.tally.violation("acl", f"{len(packet.data)}-byte ACL packet > controller "
                                 f"buffer {self.acl_data_packet_length} (a real controller "
                                 "drops it)")
        if in_flight >= self.total_num_acl_data_packets:
            self.tally.violation("acl", f"ACL packet sent with all "
                                 f"{self.total_num_acl_data_packets} controller buffers in "
                                 "use (wait for Number_Of_Completed_Packets)")
        left = st.pdu_left
        if packet.pb_flag in (0b00, 0b10):
            if left > 0:
                self.tally.violation("acl", f"new L2CAP PDU started with {left} bytes of the "
                                     "previous one missing")
            left = (struct.unpack_from("<H", packet.data)[0] + 4 - len(packet.data)
                    if len(packet.data) >= 2 else 0)
        elif packet.pb_flag == 0b01:
            if left <= 0:
                self.tally.violation("acl", "ACL continuation without a start fragment")
            left -= len(packet.data)
        if left < 0:
            self.tally.violation("acl", f"ACL data {-left} bytes beyond the L2CAP PDU length")
        st.pdu_left = max(left, 0)

    def _acl_sent(self) -> None:
        st, packet = self.air_queue.popleft()
        if st.dead:
            return                      # flushed with the link; no completion
        st.in_flight -= 1
        try:
            st.conn.assembler.feed_packet(packet)
        except Exception as error:
            if self.checks:
                self.tally.violation("acl", f"malformed L2CAP data: {error}")
        self.send_hci_packet(hci.HCI_Number_Of_Completed_Packets_Event(
            connection_handles=[st.conn.handle], num_completed_packets=[1]))


# ----------------------------------------------------------------------------
# H4 over TCP for controller A
# ----------------------------------------------------------------------------
def describe_hci(packet: bytes) -> str:
    try:
        p = hci.HCI_Packet.from_bytes(packet)
    except Exception as error:
        return f"<unparsable {packet.hex()}: {error}>"
    if isinstance(p, hci.HCI_AclDataPacket):
        d = p.data
        return (f"ACL handle=0x{p.connection_handle:03x} pb={p.pb_flag} len={len(d)} "
                f"{d[:24].hex()}{'...' if len(d) > 24 else ''}")
    return flat(p)


Watch = Callable[[Snooper.Direction, bytes], None]


class _HciClient(asyncio.Protocol):
    """One TCP connection from the stack: H4 packets to its controller"""

    def __init__(self, server: StackHciServer) -> None:
        self.server = server
        self.transport: asyncio.Transport | None = None
        self.ctrl: PeerController | None = None
        self.parser = PacketParser(self)

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        assert isinstance(transport, asyncio.Transport)
        self.transport = transport
        self.server.attach(self)

    def data_received(self, data: bytes) -> None:
        try:
            self.parser.feed_data(data)
        except core.InvalidPacketError as error:
            self.server.tally.violation("h4", f"H4 framing error ({error}): closing the "
                                        "HCI connection")
            if self.transport is not None:
                self.transport.close()

    def on_packet(self, packet: bytes) -> None:         # host -> controller
        self.server.trace(Snooper.Direction.HOST_TO_CONTROLLER, packet)
        if self.ctrl is None:
            return
        try:
            self.ctrl.on_packet(packet)
        except Exception as error:
            self.server.tally.violation("hci", f"bad HCI packet {packet[:16].hex()}...: "
                                        f"{error}")

    def connection_lost(self, exc: Exception | None) -> None:
        self.server.detach(self)


class _ToHost:
    def __init__(self, client: _HciClient) -> None:
        self.client = client

    def on_packet(self, packet: bytes) -> None:         # controller -> host
        transport = self.client.transport
        if transport is not None and not transport.is_closing():
            self.client.server.trace(Snooper.Direction.CONTROLLER_TO_HOST, packet)
            transport.write(packet)


class StackHciServer:
    """Controller A for the stack under test, as H4 over TCP; its traffic in
    a btsnoop file (for Wireshark). WATCH sees every packet."""

    def __init__(self, link: LocalLink, settings: StackSettings, tally: Tally,
                 snoop: Path, watch: Watch | None = None) -> None:
        self.link = link
        self.settings = settings
        self.tally = tally
        self.watch = watch
        self.active: _HciClient | None = None
        self.count = 0
        self.snoop_file = snoop.open("wb")
        self.snooper = BtSnooper(self.snoop_file)
        self.server: asyncio.Server | None = None

    async def start(self, host: str | None, port: int) -> None:
        self.server = await asyncio.get_running_loop().create_server(
            lambda: _HciClient(self), host, port)

    @property
    def port(self) -> int:
        """The port it listens on (the one asked for, or the one given for 0)"""
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    @property
    def controller(self) -> PeerController | None:
        """Controller A, while the stack is connected"""
        return self.active.ctrl if self.active is not None else None

    def trace(self, direction: Snooper.Direction, packet: bytes) -> None:
        if self.snoop_file.closed:
            return
        self.snooper.snoop(packet, direction)
        if self.watch is not None:
            self.watch(direction, packet)
        if h4log.isEnabledFor(logging.INFO):
            arrow = "H>C" if direction == Snooper.Direction.HOST_TO_CONTROLLER else "C>H"
            h4log.info("%s %s", arrow, describe_hci(packet))

    def attach(self, client: _HciClient) -> None:
        if self.active is not None:
            log.warning("HCI: a second host connected; dropping the first")
            old = self.active
            self.detach(old)
            if old.transport is not None:
                old.transport.close()
        self.count += 1
        s = self.settings
        ctrl = PeerController("A", self.link, s.address, self.tally, acl_size=s.acl_size,
                              acl_count=s.acl_count, air_kbps=s.acl_rate, checks=True,
                              eir=s.eir)
        ctrl.host = _ToHost(client)
        client.ctrl = ctrl
        self.active = client
        peer = client.transport.get_extra_info("peername") if client.transport else None
        log.info("HCI: host connected from %s (#%d); controller A %s attached, ACL buffers "
                 "%d x %d bytes, air rate %s", peer, self.count, s.address, s.acl_count,
                 s.acl_size, f"{s.acl_rate} kbit/s" if s.acl_rate else "unlimited")

    def detach(self, client: _HciClient) -> None:
        if client.ctrl is None:
            return
        ctrl, client.ctrl = client.ctrl, None
        if self.active is client:
            self.active = None
        ctrl.shutdown()
        self.snoop_file.flush()
        log.info("HCI: host disconnected; controller A removed (the peer sees a link loss)")

    def close(self) -> None:
        if self.active is not None:
            client = self.active
            self.detach(client)
            if client.transport is not None:
                client.transport.close()
        if self.server is not None:
            self.server.close()
        self.snoop_file.close()
