"""bt-peer hid: a DualShock 4 or a keyboard against the stack's HID host,
in the HID device role of Bumble's host.

    ds4       "Wireless Controller", SSP Just Works; streams report 0x01
              (sticks, hat, buttons) at its rate (a real one ~1000/s) until
              the host's output report 0x11 (CRC checked) sets the interval,
              then the extended report 0x11
    keyboard  "BT Keyboard", SSP Passkey Entry: it "types" the passkey the
              stack shows; boot reports (report ID 1) on every change

Virtual (run_sim): the device is controller B on the LocalLink, the stack's
controller A is served as H4 over TCP (controller.py), and with a simulator
build directory the simulator is started there ($ROCKBOX_BT_TEST "on=2 scan
connect=DEVICE", without the scan with connect) and the buttons it logs are
compared with the presses. Over the air (run_air): this PC's adapter, driven
by Bumble over raw USB (BlueZ loses it meanwhile, so nothing may be
connected to BlueZ), is the device and a player running a dev build is the
host, driven over its dev shell (bt stack on|scan|list|connect|status|forget,
log, play, health); the presses are checked against the key states its log
shows ("bt: hid keys").

Without connect the stack scans and connects to the device; with connect
the device pages the stack and opens its channels, as a paired one does.
Exit status 0 = PASS. Outputs in OUT: peer.log, sim.log, keys.json,
hci-stack.btsnoop (controller A) or hci-adapter.btsnoop (over the air),
shotNN.png (shots: the simulator's screen after each release, 3x).
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from bumble import hci, hid, l2cap
from bumble.core import PhysicalTransport
from bumble.device import Connection, Device
from bumble.host import Host
from bumble.link import LocalLink
from bumble.pairing import PairingDelegate
from bumble.snoop import BtSnooper, Snooper
from bumble.transport import open_transport
from bumble.transport.common import AsyncPipeSink
from PIL import Image

from ..shell import Shell
from . import hidkeys
from .controller import PeerController, StackHciServer
from .logs import Tally, log, peer_logging, why
from .options import HidOptions, split_hci
from .profiles import classic_device, intercept, just_works

PASSKEY_EVENT = 0x3B                # HCI_User_Passkey_Notification
PASSKEY_WAIT_S = 30.0
DISCONNECT_S = 5.0                  # for the stack to confirm the device's disconnect
SHOT_SCALE = 3
PROTOCOLS = {None: "not set", 0: "boot", 1: "report"}

type Sh = Callable[..., Awaitable[list[str]]]     # a dev shell line -> its reply lines


class PasskeyDelegate(PairingDelegate):
    """The keyboard: its user types the code the stack shows, a passkey or
    (legacy pairing) a PIN, type_delay s after it appears"""

    def __init__(self, peer: InputDevice) -> None:
        super().__init__(io_capability=PairingDelegate.KEYBOARD_INPUT_ONLY)
        self.peer = peer

    async def typed(self) -> str:
        code = await asyncio.wait_for(self.peer.passkey, PASSKEY_WAIT_S)
        await asyncio.sleep(self.peer.opts.type_delay)
        log.info("keyboard: typing %s", code)
        return code

    async def get_number(self) -> int | None:
        return int(await self.typed())

    async def get_string(self, max_length: int) -> str | None:
        return await self.typed()


class _NoSspWrite(Device):
    """legacy: SSP is off after a reset; an adapter may refuse to be told so
    (the PC's Actions adapter does, and Bumble's power-on then fails)"""

    async def send_sync_command_raw(self, command: Any) -> Any:
        if command.op_code == hci.HCI_Write_Simple_Pairing_Mode_Command.op_code:
            return hci.HCI_Command_Complete_Event(
                num_hci_command_packets=1, command_opcode=command.op_code,
                return_parameters=hci.HCI_StatusReturnParameters(hci.HCI_ErrorCode.SUCCESS))
        return await super().send_sync_command_raw(command)


class _Hid(hid.Device):
    """Bumble's HID device, saying when both channels are up"""

    def __init__(self, device: Device) -> None:
        super().__init__(device)
        self.up = asyncio.Event()

    @staticmethod
    def _name(channel: l2cap.ClassicChannel) -> str:
        return "control" if channel.psm == hid.HID_CONTROL_PSM else "interrupt"

    def on_l2cap_channel_open(self, l2cap_channel: l2cap.ClassicChannel) -> None:
        super().on_l2cap_channel_open(l2cap_channel)
        log.info("HID: %s channel open", self._name(l2cap_channel))
        if self.l2cap_ctrl_channel and self.l2cap_intr_channel:
            self.up.set()

    def on_l2cap_channel_close(self, l2cap_channel: l2cap.ClassicChannel) -> None:
        super().on_l2cap_channel_close(l2cap_channel)
        log.info("HID: %s channel closed", self._name(l2cap_channel))
        self.up.clear()


class InputDevice:
    """The input device on HOST (controller B, or the adapter with its own
    address); STACK_ADDRESS: the host it pages with connect"""

    def __init__(self, opts: HidOptions, host: Host, tally: Tally, stack_address: str,
                 address: str | None = None) -> None:
        self.opts = opts
        self.kind = opts.kind
        self.tally = tally
        self.stack_address = stack_address
        self.passkey: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self.protocol: int | None = None        # what SET_PROTOCOL asked for
        self.pressed: set[str] = set()
        self.counter = 0
        self.rate = opts.reports_per_s
        self.extended = False                   # DS4: sends report 0x11
        name, cod = hidkeys.KINDS[self.kind]
        typed = self.kind == "keyboard" and not opts.just_works
        delegate = PasskeyDelegate(self) if typed else just_works()
        self.device = classic_device(name, cod, host, opts.out / "keys.json", delegate,
                                     address=address, mitm=typed, ssp=not opts.legacy,
                                     cls=_NoSspWrite if opts.legacy else Device)
        self.device.on("connection", self.on_connection)
        self.hid = _Hid(self.device)
        self.hid.register_set_protocol_cb(self.on_set_protocol)
        self.hid.on(hid.HID.EVENT_VIRTUAL_CABLE_UNPLUG,
                    lambda: log.info("HID: the stack unplugged us"))
        self.hid.on(hid.HID.EVENT_INTERRUPT_DATA, self.on_output)
        intercept(self.device.l2cap_channel_manager, "on_l2cap_connection_request",
                  self._guard_l2cap)

    @property
    def up(self) -> asyncio.Event:
        return self.hid.up

    def _guard_l2cap(self, original: Callable[..., None]) -> Callable[..., None]:
        """HID channels need an encrypted link (security mode 4)"""
        def on_request(connection: Connection, cid: int,
                       request: l2cap.L2CAP_Connection_Request) -> None:
            if (request.psm in (hid.HID_CONTROL_PSM, hid.HID_INTERRUPT_PSM)
                    and not connection.is_encrypted):
                self.tally.violation("security", f"HID PSM 0x{request.psm:04X} opened on an "
                                     "unencrypted link")
            log.info("L2CAP: stack opens PSM 0x%04X", request.psm)
            original(connection, cid, request)
        return on_request

    def watch_passkey(self, direction: Snooper.Direction, packet: bytes) -> None:
        """Controller A's traffic: the passkey it shows the stack"""
        if (direction == Snooper.Direction.CONTROLLER_TO_HOST
                and packet[:2] == bytes([hci.HCI_EVENT_PACKET, PASSKEY_EVENT])
                and len(packet) >= 13 and not self.passkey.done()):
            passkey = f"{int.from_bytes(packet[9:13], 'little'):06d}"
            log.info("stack shows passkey %s", passkey)
            self.passkey.set_result(passkey)

    def on_connection(self, connection: Connection) -> None:
        log.info("ACL: connected to %s (%s)", connection.peer_address,
                 "we paged" if self.opts.connect else "the stack paged")
        connection.on("disconnection", lambda reason: log.info(
            "ACL: disconnected: %s", hci.HCI_Constant.error_name(reason)))

    def on_output(self, pdu: bytes) -> None:
        """DS4 output report 0x11: its interval sets the report rate"""
        out = hidkeys.ds4_output(pdu) if self.kind == "ds4" else None
        if out is None:
            log.info("HID: output %s", pdu.hex())
        elif isinstance(out, str):
            self.tally.violation("hid", out)
        else:
            log.info("HID: DS4 output: control %02x (interval %d ms), flags %02x, "
                     "light %02x%02x%02x", out.control, out.interval_ms, out.flags, *out.light)
            self.extended = True
            if out.interval_ms and self.rate:
                self.rate = 1000 / out.interval_ms

    def on_set_protocol(self, protocol: int) -> hid.Device.GetSetStatus:
        self.protocol = protocol
        refuse = self.opts.refuse_boot and protocol == 0
        log.info("HID: SET_PROTOCOL %s: %s", "boot" if protocol == 0 else "report",
                 "refused" if refuse else "ok")
        return hid.Device.GetSetStatus(
            status=hid.Device.GetSetReturn.ERR_UNSUPPORTED_REQUEST if refuse
            else hid.Device.GetSetReturn.SUCCESS)

    async def connect(self) -> None:
        """Pages the stack, secures the link, opens control then interrupt"""
        await asyncio.sleep(self.opts.connect_delay)
        log.info("ACL: paging the stack %s", self.stack_address)
        connection = await self.device.connect(
            hci.Address(self.stack_address, hci.Address.PUBLIC_DEVICE_ADDRESS),
            transport=PhysicalTransport.BR_EDR)
        await connection.authenticate()
        await connection.encrypt()
        if self.opts.silent:
            log.info("HID: opening no channels (silent)")
            return
        await self.hid.connect_control_channel()
        await self.hid.connect_interrupt_channel()
        self.up.set()

    def report(self) -> bytes:
        if self.kind == "keyboard":
            return hidkeys.keyboard_report(self.pressed)
        self.counter = (self.counter + 1) & 0x3F
        return hidkeys.ds4_report(self.pressed, self.counter, self.extended)

    def send(self) -> None:
        if self.hid.l2cap_intr_channel is not None:
            self.hid.send_data(self.report())

    async def stream(self) -> None:
        """A DS4 streams its report whether or not anything changed"""
        while self.rate:
            await self.up.wait()
            self.send()
            await asyncio.sleep(1 / self.rate)

    async def press(self, key: str) -> None:
        log.info("press %s", key)
        self.pressed.add(key)
        self.send()
        await asyncio.sleep(self.opts.hold)
        self.pressed.discard(key)
        self.send()
        log.info("release %s", key)

    async def press_all(self) -> None:
        for key in self.opts.press:
            await self.press(key)
            await asyncio.sleep(max(self.opts.step - self.opts.hold, 0))


# ----------------------------------------------------------------------------
# Against the simulator
# ----------------------------------------------------------------------------
def start_sim(opts: HidOptions, port: int) -> subprocess.Popen[bytes]:
    """The simulator in opts.sim, its HCI on PORT, its output in OUT/sim.log"""
    assert opts.sim is not None
    test = "on=2"
    if not opts.connect:            # as from the menu: the scan gives the class
        test += f" scan connect={opts.device_address}"
    if opts.shots:
        test += " shots"
        for f in (opts.sim / "simdisk").glob("dump*.bmp"):
            f.unlink()
    if opts.forget:
        (opts.sim / "simdisk/.rockbox/bluetooth.dat").unlink(missing_ok=True)
    host, _ = split_hci(opts.hci)
    env = dict(os.environ, SDL_VIDEODRIVER="offscreen", SDL_AUDIODRIVER="dummy",
               ROCKBOX_BT_TEST=test, ROCKBOX_BT_HCI=f"{host or '127.0.0.1'}:{port}")
    log.info('sim: starting %s/rockboxui, ROCKBOX_BT_TEST="%s"', opts.sim, test)
    with (opts.out / "sim.log").open("w") as out:
        return subprocess.Popen(["./rockboxui"], cwd=opts.sim, env=env, stdout=out,
                                stderr=subprocess.STDOUT, start_new_session=True)


def stop_sim(sim: subprocess.Popen[bytes]) -> None:
    """Ends the simulator started here (its own process group)"""
    try:
        os.killpg(sim.pid, signal.SIGTERM)
        sim.wait(10)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(sim.pid, signal.SIGKILL)
        sim.wait()


def save_shots(sim: Path, out: Path) -> None:
    """The simulator's screendumps (one after each release) as OUT/shotNN.png"""
    for f in out.glob("shot*.png"):
        f.unlink()
    dumps = sorted((sim / "simdisk").glob("dump*.bmp"))
    for i, f in enumerate(dumps):
        with Image.open(f) as im:
            im.resize((im.width * SHOT_SCALE, im.height * SHOT_SCALE),
                      Image.Resampling.NEAREST).save(out / f"shot{i:02d}.png")
        f.unlink()
    log.info("%d screenshots in %s", len(dumps), out)


async def run_sim(opts: HidOptions) -> int:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tally = Tally()
    link = LocalLink()
    ctrl = PeerController("B", link, opts.device_address, tally, acl_size=1021, acl_count=8,
                          le=False)
    peer = InputDevice(opts, Host(ctrl, AsyncPipeSink(ctrl)), tally, opts.stack.address,
                       address=opts.device_address)
    await peer.device.power_on()
    await peer.device.set_discoverable(True)
    await peer.device.set_connectable(True)
    server = StackHciServer(link, opts.stack, tally, opts.out / "hci-stack.btsnoop",
                            watch=peer.watch_passkey)
    host, port = split_hci(opts.hci)
    await server.start(host, port)
    log.info('peer ready: %s "%s" %s, stack HCI on %s:%d', opts.kind,
             hidkeys.KINDS[opts.kind][0], opts.device_address, host or "*", server.port)
    sim = start_sim(opts, server.port) if opts.sim else None
    tasks = [asyncio.ensure_future(peer.stream())]
    ok = False
    try:
        if opts.connect:
            tasks.append(asyncio.ensure_future(peer.connect()))
        waits = [asyncio.ensure_future(peer.up.wait()), asyncio.ensure_future(stop.wait())]
        await asyncio.wait(waits, timeout=opts.timeout, return_when=asyncio.FIRST_COMPLETED)
        tasks += waits
        if not peer.up.is_set():
            log.info("FAIL: the HID channels did not come up")
            return 1
        log.info("HID up (protocol %s)", PROTOCOLS.get(peer.protocol))
        await asyncio.sleep(1)
        await peer.press_all()
        if opts.unplug:
            peer.hid.virtual_cable_unplug()
            log.info("HID: virtual cable unplug sent")
        await asyncio.sleep(2)
        ok = True
        if opts.kind == "keyboard" and peer.protocol != 0:
            log.info("FAIL: the stack did not ask the keyboard for the boot protocol")
            ok = False
    finally:
        for t in tasks:
            t.cancel()
        if sim is not None:
            stop_sim(sim)
        server.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    if opts.sim is not None:
        outcome = hidkeys.check_sim_log((opts.out / "sim.log").read_text(errors="replace"),
                                        opts.press, opts.buttons)
        for line in outcome.notes:
            log.info("%s", line)
        ok = outcome.ok and ok
        if opts.shots:
            save_shots(opts.sim, opts.out)
    log.info("%s", "PASS" if ok else "FAIL")
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# Over the air: this PC's adapter is the device, a player the host
# ----------------------------------------------------------------------------
def bluez_busy() -> str:
    """The devices BlueZ has connected (the adapter stays with them)"""
    try:
        return subprocess.run(["bluetoothctl", "devices", "Connected"], capture_output=True,
                              text=True, timeout=20, check=False).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


async def run_air(opts: HidOptions, shell: Shell, adapter: str, player: str) -> int:
    """ADAPTER (a Bumble transport) as the device for PLAYER (its BD_ADDR),
    which runs a dev build: its shell turns Bluetooth on and connects"""
    busy = bluez_busy()
    if busy:
        log.info("FAIL: BlueZ has a device connected (%s): the adapter stays with it", busy)
        return 1
    loop = asyncio.get_running_loop()

    async def sh(line: str, quiet: bool = False) -> list[str]:
        lines = (await loop.run_in_executor(None, shell, line)).lines
        if not quiet:
            for text in lines:
                log.info("player: %s", text)
        return lines

    tally = Tally()
    await sh("bt stack on")
    ok = False
    async with await open_transport(adapter) as transport:
        with (opts.out / "hci-adapter.btsnoop").open("wb") as snoop:
            host = Host(transport.source, transport.sink)
            host.snooper = BtSnooper(snoop)
            peer = InputDevice(opts, host, tally, player)
            await peer.device.power_on()
            await peer.device.set_discoverable(not opts.connect)
            await peer.device.set_connectable(True)
            addr = str(peer.device.public_address).split("/")[0]
            log.info("adapter %s is the %s", addr, opts.kind)
            stream = asyncio.ensure_future(peer.stream())
            try:
                if opts.forget:
                    await sh(f"bt stack forget {addr}")
                if opts.connect:
                    await peer.connect()
                else:
                    await _player_connects(sh, peer, addr)
                await asyncio.wait_for(peer.up.wait(), opts.timeout)
                log.info("HID up (protocol %s)", PROTOCOLS.get(peer.protocol))
                if opts.play:
                    await sh(f"play {opts.play}")
                await asyncio.sleep(1)
                await peer.press_all()
                await asyncio.sleep(1)
                outcome = hidkeys.check_player_log(await sh("log", quiet=True), opts.press)
                for line in outcome.notes:
                    log.info("%s", line)
                ok = outcome.ok
                if opts.kind == "keyboard" and peer.protocol != 0:
                    log.info("FAIL: the player did not ask the keyboard for the boot protocol")
                    ok = False
                for t in range(0, int(opts.stay), 10):
                    await asyncio.sleep(min(10, opts.stay - t))
                    await sh("bt stack status")
                    await sh("health")
                    if not peer.up.is_set():
                        log.info("FAIL: the HID link went down")
                        ok = False
                        break
            finally:
                stream.cancel()
                await _hang_up(peer.device)
    log.info("%s", "PASS" if ok else "FAIL")
    return 0 if ok else 1


async def _hang_up(device: Device) -> None:
    """End the links as a controller switched off does (0x13). Without it
    the player holds the link until its supervision timeout (0x08, ~20 s),
    and a torture took it for the link to this PC, whose address it has."""
    for connection in list(device.connections.values()):
        try:
            await asyncio.wait_for(connection.disconnect(), DISCONNECT_S)
        except Exception as error:
            log.info("disconnect: %s", why(error))


async def _player_connects(sh: Sh, peer: InputDevice, addr: str) -> None:
    """The player scans (inquiry, then the names), connects to ADDR, and
    shows a pairing code if one is needed: the keyboard types it"""
    await sh("bt stack scan")
    for _ in range(40):
        await asyncio.sleep(1)
        if any("scanning 0" in line for line in await sh("bt stack list", quiet=True)):
            break
    await sh("bt stack list")
    await sh(f"bt stack connect {addr}")
    for _ in range(20):
        if peer.up.is_set():
            break
        for line in await sh("bt stack status", quiet=True):
            m = re.search(r"pairing code (\d+)", line)
            if m and not peer.passkey.done():
                log.info("player shows pairing code %s", m.group(1))
                peer.passkey.set_result(m.group(1))
        await asyncio.sleep(2)          # each poll logs on the player


def _prepare(opts: HidOptions) -> None:
    opts.out.mkdir(parents=True, exist_ok=True)
    if opts.forget:
        (opts.out / "keys.json").unlink(missing_ok=True)


def main(opts: HidOptions) -> int:
    """The virtual device (and the simulator, if opts.sim)"""
    _prepare(opts)
    with peer_logging(opts.out / "peer.log", opts.verbose, opts.trace):
        return asyncio.run(run_sim(opts))


def main_air(opts: HidOptions, shell: Shell, adapter: str, player: str, *,
             limit: float | None = None, echo: bool = True) -> int:
    """Over the air; TimeoutError after LIMIT s. ECHO: the log on stdout too."""
    _prepare(opts)
    with peer_logging(opts.out / "peer.log", opts.verbose, echo=echo):
        return asyncio.run(asyncio.wait_for(run_air(opts, shell, adapter, player), limit))

