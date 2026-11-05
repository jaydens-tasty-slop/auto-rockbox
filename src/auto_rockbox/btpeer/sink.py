"""bt-peer sink: a virtual A2DP sink with an AVRCP controller (and, if
asked, an HFP hands-free unit) against the stack under test in the
simulator. Controller A is served to the simulator as H4 over TCP; the sink
"Bumble Sink" (headphones, classic only, SSP Just Works) runs on controller
B with one SBC endpoint, as strict as a real headset: it rejects bad
configurations, refuses AVDTP/AVCTP/RFCOMM on an unencrypted link, and
checks every media packet (media.py). After --seconds of audio the stream
(OUT/stream.sbc) is decoded with ffmpeg (OUT/stream.wav) and judged: tone,
dropouts, stalls, real-time rate, RTP/SBC problems, stack violations.

Either side may start: by default the stack pages the sink and configures
the stream; with connect the sink pages the stack (and with configure also
configures and opens the stream). Timed steps (options.Step) press AVRCP
keys, change the sink's volume, report the HF battery, slow controller A's
air rate, drop the link, or leave AVDTP requests unanswered; the drop_*
options leave the stack's first requests unanswered.

Outputs in OUT: peer.log, stream.sbc, stream.wav, keys.json and
hci-stack.btsnoop (controller A's HCI traffic).

The stack side is a simulator build started in its build directory once
the peer is up, with $ROCKBOX_BT_HCI naming the peer's address (default
127.0.0.1:6402) and $ROCKBOX_BT_TEST its test words (bt-simtest.c: on,
scan, connect=ADDR, play=FILE, off=N, repeat=N), e.g. with a 1 kHz tone in
simdisk:
    SDL_VIDEODRIVER=offscreen SDL_AUDIODRIVER=dummy timeout 90 \\
      env ROCKBOX_BT_TEST="connect=F0:F0:F0:F0:F0:02 play=/tone.mp3 off=72" ./rockboxui
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import subprocess
import time
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from bumble import a2dp, avc, avctp, avdtp, avrcp, core, hci, hfp, l2cap, rfcomm, sdp
from bumble.core import PhysicalTransport
from bumble.device import Connection
from bumble.host import Host
from bumble.link import LocalLink
from bumble.transport.common import AsyncPipeSink

from .analysis import WavAnalysis, analyse_wav
from .controller import PeerController, StackHciServer
from .logs import Tally, flat, log, peer_logging, why
from .media import Recorder, judge
from .options import SinkOptions, Step, split_hci
from .profiles import (
    HEADPHONES_COD,
    SBC,
    authenticate,
    classic_device,
    intercept,
    just_works,
    sbc_element,
    sink_capabilities,
    sink_records,
)

SINK_NAME = "Bumble Sink"
STEP_S = 5.0                # an AVDTP step of the sink's own configuration
AVRCP_FALLBACK_S = 3.0      # the stack connects AVRCP within this of the stream opening
KEY_RESPONSE_S = 1.0
PASSTHROUGH = {
    "play": avc.PassThroughFrame.OperationId.PLAY,
    "pause": avc.PassThroughFrame.OperationId.PAUSE,
    "stop": avc.PassThroughFrame.OperationId.STOP,
    "next": avc.PassThroughFrame.OperationId.FORWARD,
    "previous": avc.PassThroughFrame.OperationId.BACKWARD,
    "volup": avc.PassThroughFrame.OperationId.VOLUME_UP,
    "voldown": avc.PassThroughFrame.OperationId.VOLUME_DOWN,
    "ffwd": avc.PassThroughFrame.OperationId.FAST_FORWARD,
    "rewind": avc.PassThroughFrame.OperationId.REWIND,
}
_CAT = avdtp.ServiceCategory


def check_configuration(configuration: list[avdtp.ServiceCapabilities]
                        ) -> tuple[avdtp.ServiceCategory, avdtp.ErrorCode, str] | None:
    """A SET_CONFIGURATION as a strict sink judges it: (category, error code,
    why), or None if it is accepted"""
    codec = None
    if _CAT.MEDIA_TRANSPORT not in [c.service_category for c in configuration]:
        return _CAT.MEDIA_TRANSPORT, avdtp.ErrorCode.INVALID_CAPABILITIES, "no Media Transport"
    for c in configuration:
        if c.service_category == _CAT.MEDIA_CODEC:
            codec = c
        elif c.service_category != _CAT.MEDIA_TRANSPORT:
            category = avdtp.ServiceCategory(c.service_category)
            return (category, avdtp.ErrorCode.UNSUPPORTED_CONFIGURATION,
                    f"{category.name} was not in the sink capabilities")
    if codec is None:
        return _CAT.MEDIA_CODEC, avdtp.ErrorCode.INVALID_CAPABILITIES, "no Media Codec"
    if (not isinstance(codec, avdtp.MediaCodecCapabilities)
            or codec.media_type != avdtp.AVDTP_AUDIO_MEDIA_TYPE
            or codec.media_codec_type != a2dp.A2DP_SBC_CODEC_TYPE
            or not isinstance(codec.media_codec_information, SBC)):
        return _CAT.MEDIA_CODEC, avdtp.ErrorCode(0xC2), "codec is not SBC"
    if err := sbc_element(codec.media_codec_information).problem():
        return _CAT.MEDIA_CODEC, avdtp.ErrorCode(err[0]), err[1]
    return None


def _codec(configuration: list[avdtp.ServiceCapabilities]) -> SBC:
    """The SBC element of a configuration check_configuration accepted"""
    for c in configuration:
        if (isinstance(c, avdtp.MediaCodecCapabilities)
                and isinstance(c.media_codec_information, SBC)):
            return c.media_codec_information
    raise ValueError("no SBC codec in the configuration")


class StrictSink(avdtp.LocalSink):
    def __init__(self, protocol: avdtp.Protocol, seid: int, app: Sink) -> None:
        super().__init__(protocol, seid, sink_capabilities())
        self.app = app

    def _check(self, configuration: list[avdtp.ServiceCapabilities], what: str
               ) -> tuple[avdtp.ServiceCategory, avdtp.ErrorCode] | None:
        if err := check_configuration(configuration):
            category, code, reason = err
            log.warning("AVDTP %s rejected: %s (category %s, error 0x%02X)", what, reason,
                        category.name, code)
            return category, code
        info = _codec(configuration)
        log.info("AVDTP %s accepted: %s", what, sbc_element(info).config())
        self.configuration = configuration
        self.app.configured(info)
        return None

    async def on_set_configuration_command(
            self, configuration: Any) -> avdtp.Message | None:
        if err := self._check(list(configuration), "SET_CONFIGURATION"):
            return avdtp.Set_Configuration_Reject(service_category=err[0], error_code=err[1])
        self.emit(self.EVENT_CONFIGURATION)
        return None

    async def on_reconfigure_command(self, command: Any) -> avdtp.Message | None:
        transport = [c for c in self.configuration
                     if c.service_category == _CAT.MEDIA_TRANSPORT]
        if err := self._check(transport + list(command), "RECONFIGURE"):
            return avdtp.Reconfigure_Reject(service_category=err[0], error_code=err[1])
        return None

    def on_rtp_channel_open(self) -> None:
        stream = self.stream
        assert stream is not None and stream.rtp_channel is not None
        channel = stream.rtp_channel
        log.info("AVDTP media channel open, L2CAP MTU in/out %d/%d", channel.mtu,
                 channel.peer_mtu)
        recorder = self.app.recorder

        def on_media(pdu: bytes) -> None:
            state = self.stream.state if self.stream else None
            recorder.on_media(pdu, channel.mtu, getattr(state, "name", str(state)))
        channel.sink = on_media
        self.emit(self.EVENT_RTP_CHANNEL_OPEN)


class VolumeDelegate(avrcp.Delegate):
    """abs_volume: an AVRCP target with absolute volume (volume event) that
    logs the stack's SET ABSOLUTE VOLUME commands"""

    def __init__(self, volume: int) -> None:
        super().__init__(supported_events=[avrcp.EventId.VOLUME_CHANGED])
        self.volume = volume
        self.sets: list[tuple[float, int]] = []

    async def set_absolute_volume(self, volume: int) -> None:
        log.info("AVRCP: stack set absolute volume %d", volume)
        self.sets.append((time.monotonic(), volume))
        self.volume = volume


class SinkAvrcp(avrcp.Protocol):
    """Logs the stack's commands, leaves the first DROP unanswered, and
    sends passthrough keys"""

    def __init__(self, delegate: avrcp.Delegate, drop: int) -> None:
        super().__init__(delegate)
        self.drop = drop

    def _on_avctp_command(self, transaction_label: int, payload: bytes) -> None:
        log.info("AVRCP <- command from the stack: %s", flat(avc.Frame.from_bytes(payload)))
        if self.drop:
            self.drop -= 1
            log.info("AVRCP: command left unanswered (drop_avrcp)")
            return
        super()._on_avctp_command(transaction_label, payload)

    async def passthrough(self, key: avc.PassThroughFrame.OperationId, pressed: bool) -> str:
        """One press or release; the stack's response"""
        frame = avc.PassThroughCommandFrame(
            avc.CommandFrame.CommandType.CONTROL, avc.Frame.SubunitType.PANEL, 0,
            avc.PassThroughFrame.StateFlag.PRESSED if pressed
            else avc.PassThroughFrame.StateFlag.RELEASED, key, b"")
        pending = await self._obtain_pending_command()
        self.send_command(pending.transaction_label, frame)
        try:
            response = await asyncio.wait_for(asyncio.shield(pending.response), KEY_RESPONSE_S)
        except TimeoutError:
            if self.pending_commands.get(pending.transaction_label) is pending:
                self.recycle_pending_command(pending)
            return f"NO RESPONSE ({KEY_RESPONSE_S:g} s)"
        except Exception as error:
            return f"error {why(error)}"
        result = getattr(response.response, "name", str(response.response))
        op = getattr(response, "operation_id", key)
        if op != key:
            result += f" (for {getattr(op, 'name', op)}!)"
        return result


class _RoleHost(Host):
    """sink_central: accepts the stack's page as central, as BlueZ does (its
    AVDTP socket asks for the central role). sink_no_switch: pages the stack
    with role switches not allowed, so the stack's request for the central
    role is refused."""

    def __init__(self, ctrl: PeerController, central: bool, no_switch: bool) -> None:
        super().__init__(ctrl, AsyncPipeSink(ctrl))
        self.central = central
        self.no_switch = no_switch

    async def send_async_command(self, command: hci.HCI_AsyncCommand, check_status: bool = True,
                                 response_timeout: float | None = None) -> hci.HCI_ErrorCode:
        if self.central and isinstance(command, hci.HCI_Accept_Connection_Request_Command):
            command = hci.HCI_Accept_Connection_Request_Command(bd_addr=command.bd_addr,
                                                                role=hci.Role.CENTRAL)
        elif self.no_switch and isinstance(command, hci.HCI_Create_Connection_Command):
            command.allow_role_switch = 0
        return await super().send_async_command(command, check_status, response_timeout)


class Sink:
    """The sink device on controller B and everything it does in a run"""

    def __init__(self, opts: SinkOptions, link: LocalLink, recorder: Recorder,
                 tally: Tally) -> None:
        self.opts = opts
        self.recorder = recorder
        self.tally = tally
        self.connection: Connection | None = None
        self.protocol: avdtp.Protocol | None = None
        self.endpoint: StrictSink | None = None
        self.started_at: float | None = None
        self.key_results: list[tuple[float, str, str]] = []
        self.stack_server: StackHciServer | None = None     # for rate= steps
        self.stopping = False
        self.tasks: set[asyncio.Future[Any]] = set()
        self.connect_task: asyncio.Future[Any] | None = None
        self.avrcp_incoming = False
        self.avrcp_ready = asyncio.Event()
        self.hf: hfp.HfProtocol | None = None
        self.hf_status = "never connected"
        self.hf_reports: list[tuple[float, int, str]] = []
        self.hf_xapl = False
        self.discovers_to_drop = opts.drop_discover
        self.avdtp_opens_to_drop = opts.drop_avdtp_open
        self.ctrl = PeerController("B", link, opts.sink_address, tally, acl_size=1021,
                                   acl_count=64, le=False)
        self.device = classic_device(
            SINK_NAME, HEADPHONES_COD,
            _RoleHost(self.ctrl, opts.sink_central, opts.sink_no_switch),
            opts.out / "keys.json", just_works(), address=opts.sink_address)
        self.device.sdp_service_records = sink_records(opts.abs_volume is not None)
        self.hf_config = hfp.HfConfiguration(
            supported_hf_features=[hfp.HfFeature.HF_INDICATORS,
                                   hfp.HfFeature.REMOTE_VOLUME_CONTROL],
            supported_hf_indicators=[hfp.HfIndicator.BATTERY_LEVEL],
            supported_audio_codecs=[hfp.AudioCodec.CVSD])
        if opts.hf is not None:
            channel = rfcomm.Server(self.device).listen(
                lambda dlc: self.start_hf(dlc, "stack-initiated"))
            self.device.sdp_service_records[0x00010004] = hfp.make_hf_sdp_records(
                0x00010004, channel, self.hf_config, hfp.ProfileVersion.V1_7)
        self.device.on("connection", self.on_connection)
        self.device.on("connection_failure",
                       lambda e: log.info("ACL: connection failed: %s", e))
        # AVDTP: the sink's own L2CAP server, so its MTU is configurable
        self.listener = avdtp.Listener()
        server = self.device.create_l2cap_server(
            spec=l2cap.ClassicChannelSpec(psm=avdtp.AVDTP_PSM, mtu=opts.mtu))
        server.on("connection", self.listener.on_l2cap_connection)
        self.listener.on("connection", lambda p: self.on_avdtp(p, outgoing=False))
        # AVRCP controller (Bumble's protocol also answers target commands,
        # with absolute volume if asked)
        self.volume = VolumeDelegate(opts.abs_volume) if opts.abs_volume is not None else None
        self.avrcp = SinkAvrcp(self.volume or avrcp.Delegate(), opts.drop_avrcp)
        self.avrcp.listen(self.device)
        self.avrcp.on("connection", self.on_avrcp_incoming)
        self.avrcp.on("start", self.on_avrcp_start)
        self.avrcp.on("stop", self.on_avrcp_stop)
        intercept(self.device.l2cap_channel_manager, "on_l2cap_connection_request",
                  self._guard_l2cap)

    def _guard_l2cap(self, original: Callable[..., None]) -> Callable[..., None]:
        """Security mode 4: AVDTP, AVCTP and RFCOMM need an encrypted link;
        drop_avdtp_open leaves AVDTP requests unanswered"""
        manager = self.device.l2cap_channel_manager

        def on_request(connection: Connection, cid: int,
                       request: l2cap.L2CAP_Connection_Request) -> None:
            if (not self.opts.allow_unencrypted and not connection.is_encrypted
                    and request.psm in (avdtp.AVDTP_PSM, avctp.AVCTP_PSM, rfcomm.RFCOMM_PSM)):
                self.tally.violation("security", f"L2CAP connect to PSM 0x{request.psm:04X} on "
                                     "an unencrypted link: refused (security block); "
                                     "authenticate + encrypt first")
                manager.send_control_frame(connection, cid, l2cap.L2CAP_Connection_Response(
                    identifier=request.identifier, destination_cid=0,
                    source_cid=request.source_cid,
                    result=l2cap.L2CAP_Connection_Response.Result
                    .CONNECTION_REFUSED_SECURITY_BLOCK,
                    status=0))
                return
            if request.psm == avdtp.AVDTP_PSM and self.avdtp_opens_to_drop:
                self.avdtp_opens_to_drop -= 1
                log.info("L2CAP: AVDTP connect from the stack left unanswered")
                return
            log.info("L2CAP: stack opens PSM 0x%04X", request.psm)
            original(connection, cid, request)
        return on_request

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Future[Any]:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    # -- ACL / security --------------------------------------------------------
    def on_connection(self, conn: Connection) -> None:
        log.info("ACL: connected to %s, handle 0x%03x, sink is %s", conn.peer_address,
                 conn.handle, conn.role_name)
        self.connection = conn

        def on_disconnection(reason: int) -> None:
            log.info("ACL: disconnected (%s)", hci.HCI_Constant.error_name(reason))
            if self.connection is conn:
                self.connection = None
            self.hf = None
            self.hf_xapl = False
            if self.opts.connect and not self.stopping:
                self.start_connect_loop(2.0)
        conn.on("disconnection", on_disconnection)
        conn.on("connection_encryption_change", lambda: log.info(
            "security: encryption %s", "on" if conn.is_encrypted else "off"))
        conn.on("connection_authentication", lambda: log.info("security: authenticated"))
        conn.on("connection_authentication_failure", lambda e: log.info(
            "security: authentication failed (%s)", hci.HCI_Constant.error_name(e)))
        conn.on("classic_pairing", lambda: log.info("security: paired (SSP)"))
        conn.on("classic_pairing_failure", lambda e: log.info(
            "security: pairing failed (%s)", hci.HCI_Constant.error_name(e)))
        conn.on("link_key", lambda: log.info("security: new link key stored in %s",
                                             self.opts.out / "keys.json"))
        if self.opts.sdp and not self.opts.connect:
            self.spawn(self.query_sdp(conn))

    def start_connect_loop(self, delay: float) -> None:
        if self.connect_task is None or self.connect_task.done():
            self.connect_task = self.spawn(self.connect_loop(delay))

    async def connect_loop(self, delay: float) -> None:
        await asyncio.sleep(delay)
        deadline = time.monotonic() + self.opts.connect_timeout
        address = hci.Address(self.opts.stack.address, hci.Address.PUBLIC_DEVICE_ADDRESS)
        while not self.stopping and self.connection is None:
            log.info("ACL: sink pages the stack at %s", address)
            try:
                conn = await asyncio.wait_for(
                    self.device.connect(address, transport=PhysicalTransport.BR_EDR), 20)
            except Exception as error:
                log.info("ACL: page failed: %s", why(error))
                if time.monotonic() > deadline:
                    log.warning("ACL: giving up after connect_timeout %g s",
                                self.opts.connect_timeout)
                    return
                await asyncio.sleep(2)
                continue
            try:
                await self.setup_outgoing(conn)
            except Exception as error:
                log.warning("sink-initiated setup failed: %s", flat(error) or repr(error))
            return

    async def setup_outgoing(self, conn: Connection) -> None:
        log.info("security: sink authenticates")
        await authenticate(self.device, conn)
        log.info("security: sink enables encryption")
        await conn.encrypt()
        if self.opts.sdp:
            await self.query_sdp(conn)
        await asyncio.sleep(0.5)
        if conn.handle in self.listener.servers:
            log.info("AVDTP: the stack already opened signalling; using its channel")
            protocol = self.listener.servers[conn.handle]
        else:
            log.info("AVDTP: sink opens the signalling channel")
            channel = await conn.create_l2cap_channel(
                spec=l2cap.ClassicChannelSpec(psm=avdtp.AVDTP_PSM, mtu=self.opts.mtu))
            protocol = avdtp.Protocol(channel, (1, 3))
            self.listener.set_server(conn, protocol)
            self.on_avdtp(protocol, outgoing=True)
        if self.opts.configure:
            await self.configure_stream(conn, protocol)
        else:
            log.info("AVDTP: waiting for the source to discover and configure")

    async def configure_stream(self, conn: Connection, protocol: avdtp.Protocol) -> None:
        def step[T](aw: Coroutine[Any, Any, T]) -> Coroutine[Any, Any, T]:
            return asyncio.wait_for(aw, STEP_S)
        source = info = None
        for ep in await step(protocol.discover_remote_endpoints()):
            log.info("AVDTP: stack endpoint SEID %d %s, %s, in use %d: %s", ep.seid,
                     getattr(ep.tsep, "name", ep.tsep),
                     getattr(ep.media_type, "name", ep.media_type), ep.in_use,
                     " / ".join(flat(c) for c in ep.capabilities))
            for cap in ep.capabilities:
                if (ep.tsep == avdtp.AVDTP_TSEP_SRC and not ep.in_use and source is None
                        and isinstance(cap, avdtp.MediaCodecCapabilities)
                        and cap.media_codec_type == a2dp.A2DP_SBC_CODEC_TYPE
                        and isinstance(cap.media_codec_information, SBC)):
                    source, info = ep, cap.media_codec_information
        if source is None or info is None or self.endpoint is None:
            log.warning("AVDTP: the stack has no free SBC source endpoint")
            return

        def choose[F: int](have: int, prefs: tuple[F, ...]) -> F:
            return next((p for p in prefs if have & p), prefs[-1])
        cfg = SBC(
            sampling_frequency=choose(info.sampling_frequency, (
                SBC.SamplingFrequency.SF_44100, SBC.SamplingFrequency.SF_48000)),
            channel_mode=choose(info.channel_mode, (
                SBC.ChannelMode.JOINT_STEREO, SBC.ChannelMode.STEREO,
                SBC.ChannelMode.DUAL_CHANNEL, SBC.ChannelMode.MONO)),
            block_length=choose(info.block_length, (
                SBC.BlockLength.BL_16, SBC.BlockLength.BL_12, SBC.BlockLength.BL_8,
                SBC.BlockLength.BL_4)),
            subbands=choose(info.subbands, (SBC.Subbands.S_8, SBC.Subbands.S_4)),
            allocation_method=choose(info.allocation_method, (
                SBC.AllocationMethod.LOUDNESS, SBC.AllocationMethod.SNR)),
            minimum_bitpool_value=max(2, info.minimum_bitpool_value),
            maximum_bitpool_value=min(53, info.maximum_bitpool_value))
        configuration = [
            avdtp.ServiceCapabilities(avdtp.AVDTP_MEDIA_TRANSPORT_SERVICE_CATEGORY),
            avdtp.MediaCodecCapabilities(media_type=avdtp.AVDTP_AUDIO_MEDIA_TYPE,
                                         media_codec_type=a2dp.A2DP_SBC_CODEC_TYPE,
                                         media_codec_information=cfg)]
        sink = self.endpoint
        sink.configuration = configuration
        stream = avdtp.Stream(protocol, sink, source)
        protocol.streams[sink.seid] = stream
        log.info("AVDTP: sink configures SEID %d: %s", source.seid, sbc_element(cfg).config())
        await step(source.set_configuration(sink.seid, configuration))
        stream.change_state(avdtp.State.CONFIGURED)
        self.configured(cfg)
        await step(source.open())
        stream.change_state(avdtp.State.OPEN)
        self.stream_opened()
        channel = await conn.create_l2cap_channel(
            spec=l2cap.ClassicChannelSpec(psm=avdtp.AVDTP_PSM, mtu=self.opts.mtu))
        stream.rtp_channel = channel
        channel.on("close", stream.on_l2cap_channel_close)
        sink.on_rtp_channel_open()
        log.info("AVDTP: stream open (sink-initiated); waiting for the source to START")

    # -- AVDTP -----------------------------------------------------------------
    def on_avdtp(self, protocol: avdtp.Protocol, outgoing: bool) -> None:
        channel = protocol.l2cap_channel
        log.info("AVDTP: signalling channel open (%s), L2CAP MTU in/out %d/%d",
                 "sink-initiated" if outgoing else "stack-initiated", channel.mtu,
                 channel.peer_mtu)
        received = protocol.message_assembler.callback

        def on_message(label: int, message: avdtp.Message) -> None:
            log.info("AVDTP <- [%d] %s", label, flat(message))
            received(label, message)
        protocol.message_assembler.callback = on_message

        def log_sent(sent: Callable[..., None]) -> Callable[..., None]:
            def send_message(label: int, message: avdtp.Message) -> None:
                log.info("AVDTP -> [%d] %s", label, flat(message))
                sent(label, message)
            return send_message
        intercept(protocol, "send_message", log_sent)

        def drop_discovers(answer: Callable[..., Any]) -> Callable[..., Any]:
            def on_discover_command(command: avdtp.Discover_Command) -> Any:
                if self.discovers_to_drop:
                    self.discovers_to_drop -= 1
                    log.info("AVDTP: DISCOVER left unanswered")
                    return None     # Bumble then sends no response
                return answer(command)
            return on_discover_command
        intercept(protocol, "on_discover_command", drop_discovers)
        endpoint = StrictSink(protocol, len(protocol.local_endpoints) + 1, self)
        protocol.local_endpoints.append(endpoint)
        self.protocol, self.endpoint = protocol, endpoint
        endpoint.on("open", self.stream_opened)
        endpoint.on("start", self.stream_started)
        endpoint.on("suspend", lambda: log.info("AVDTP: stream suspended by the source"))
        endpoint.on("close", lambda: log.info("AVDTP: stream closed by the source"))
        endpoint.on("abort", lambda: log.info("AVDTP: stream aborted"))
        endpoint.on("rtp_channel_close", lambda: log.info("AVDTP: media channel closed"))
        endpoint.on("delay_report", lambda d: log.info("AVDTP: delay report %d", d))
        protocol.on("close", lambda: log.info("AVDTP: signalling channel closed"))

    def configured(self, info: SBC) -> None:
        self.recorder.new_stream(sbc_element(info).config())

    def stream_opened(self) -> None:
        log.info("AVDTP: stream open")
        if self.connection is not None:
            self.spawn(self.avrcp_fallback(self.connection))
            if self.opts.hf is not None and self.opts.hf_connect:
                self.spawn(self.hf_connect(self.connection))

    def stream_started(self) -> None:
        log.info("AVDTP: stream started")
        if self.started_at is not None:
            self.recorder.restart()
            return
        self.started_at = time.monotonic()
        if self.opts.keys:
            self.spawn(self.run_steps(self.started_at))
        if self.opts.suspend is not None or self.opts.disconnect is not None:
            self.spawn(self.sink_actions(self.started_at))

    # -- HFP (hands-free side) -------------------------------------------------
    def start_hf(self, dlc: rfcomm.DLC, how: str) -> None:
        if self.hf is not None:
            log.warning("HFP: a second RFCOMM DLC (%s) ignored", how)
            return
        log.info("HFP: RFCOMM DLC open (%s)", how)
        self.hf_status = f"{how}, service level connection pending"
        hf = self.hf = hfp.HfProtocol(dlc, self.hf_config)
        read_at = dlc.sink

        def sink(data: bytes) -> None:
            log.info("HFP <- %r", bytes(data))
            if read_at is not None:
                read_at(data)
        dlc.sink = sink
        self.spawn(self.hf_run(hf, how))

    async def hf_run(self, hf: hfp.HfProtocol, how: str) -> None:
        try:
            await hf.initiate_slc()
        except Exception as error:
            self.hf_status = f"{how}, service level connection FAILED: {why(error)}"
            log.warning("HFP: %s", self.hf_status)
            return
        self.hf_status = (f"{how}, service level connection up (AG features "
                          f"0x{hf.supported_ag_features:x})")
        log.info("HFP: %s", self.hf_status)
        self.spawn(hf.run())
        if self.opts.hf is not None:
            await self.hf_report(hf, self.opts.hf)

    async def hf_report(self, hf: hfp.HfProtocol, percent: int) -> None:
        """Sends the battery level, as the stack should read it back"""
        try:
            if self.opts.hf_apple:
                if not self.hf_xapl:
                    await hf.execute_command("AT+XAPL=ABCD-1234-0100,2")
                    self.hf_xapl = True
                level = max(0, min(9, round(percent / 10) - 1))
                await hf.execute_command(f"AT+IPHONEACCEV=1,1,{level}")
                expect = (level + 1) * 10
            else:
                state = hf.hf_indicators[hfp.HfIndicator.BATTERY_LEVEL]
                if not state.enabled:
                    log.warning("HFP: the AG did not enable the battery indicator")
                    self.hf_reports.append((time.monotonic(), percent, "not enabled"))
                    return
                await hf.execute_command(f"AT+BIEV=2,{percent}")
                expect = percent
        except Exception as error:
            self.hf_reports.append((time.monotonic(), percent, f"error {why(error)}"))
            return
        log.info("HFP: battery %d%% reported (the stack should show %d%%)", percent, expect)
        self.hf_reports.append((time.monotonic(), expect, "OK"))

    async def hf_connect(self, conn: Connection) -> None:
        await asyncio.sleep(0.5)
        if self.hf is not None or conn is not self.connection:
            return
        try:
            found = await hfp.find_ag_sdp_record(conn)
            if not found:
                self.hf_status = "the stack has no AG record"
                log.warning("HFP: %s", self.hf_status)
                return
            channel, version, features = found
            log.info("HFP: the stack's AG record: channel %d, version %s, features %s",
                     channel, version, features)
            mux = await rfcomm.Client(conn).start()
            dlc = await mux.open_dlc(channel)
        except Exception as error:
            self.hf_status = f"sink connect FAILED: {why(error)}"
            log.warning("HFP: %s", self.hf_status)
            return
        self.start_hf(dlc, "sink-initiated")

    # -- AVRCP -----------------------------------------------------------------
    def on_avrcp_incoming(self) -> None:
        self.avrcp_incoming = True

    def on_avrcp_start(self) -> None:
        log.info("AVRCP: AVCTP channel open (%s)",
                 "stack-initiated" if self.avrcp_incoming else "sink-initiated")
        self.avrcp_incoming = False
        self.avrcp_ready.set()
        if self.opts.avrcp_query:
            self.spawn(self.avrcp_query())

    def on_avrcp_stop(self) -> None:
        log.info("AVRCP: AVCTP channel closed")
        self.avrcp_ready.clear()

    async def avrcp_fallback(self, conn: Connection) -> None:
        await asyncio.sleep(AVRCP_FALLBACK_S)
        if self.avrcp.avctp_protocol is not None or conn is not self.connection:
            return
        log.info("AVRCP: the stack did not connect within %g s of the stream opening; "
                 "connecting from the sink", AVRCP_FALLBACK_S)
        try:
            await self.avrcp.connect(conn)
        except Exception as error:
            log.warning("AVRCP: connect failed: %s", flat(error) or repr(error))

    async def avrcp_query(self) -> None:
        calls: tuple[tuple[str, Callable[[], Coroutine[Any, Any, object]]], ...] = (
            ("GetCapabilities(EVENTS_SUPPORTED)", self.avrcp.get_supported_events),
            ("GetPlayStatus", self.avrcp.get_play_status))
        for name, call in calls:
            try:
                log.info("AVRCP: %s -> %s", name, flat(await asyncio.wait_for(call(), 2)))
            except Exception as error:
                log.info("AVRCP: %s -> %s", name, why(error))
        try:
            async for status in self.avrcp.monitor_playback_status():
                log.info("AVRCP: PLAYBACK_STATUS_CHANGED -> %s", flat(status))
        except Exception as error:
            log.info("AVRCP: RegisterNotification(PLAYBACK_STATUS_CHANGED) -> %s", why(error))

    async def run_steps(self, t0: float) -> None:
        for step in sorted(self.opts.keys, key=lambda s: s.t):
            await asyncio.sleep(max(0.0, t0 + step.t - time.monotonic()))
            result = await self.do_step(step)
            self.key_results.append((step.t, str(step), result))

    async def do_step(self, step: Step) -> str:
        t, v = step.t, step.value or 0
        match step.action:
            case "batt":
                if self.hf is None:
                    return "no HFP link"
                await self.hf_report(self.hf, v)
                return self.hf_reports[-1][2]
            case "rate":
                ctrl = self.stack_server.controller if self.stack_server else None
                if ctrl is None:
                    return "no stack controller"
                ctrl.air_kbps = v
                log.info("HCI: controller A air rate %s at +%gs",
                         f"{v} kbit/s" if v else "unlimited", t)
                return "set"
            case "nodiscover":
                self.discovers_to_drop = v
                log.info("AVDTP: the next %d DISCOVERs go unanswered (+%gs)", v, t)
                return "set"
            case "noopen":
                self.avdtp_opens_to_drop = v
                log.info("L2CAP: the next %d AVDTP requests go unanswered (+%gs)", v, t)
                return "set"
            case "lose":
                self.ctrl.lose_links()
                log.info("LINK: lost at +%gs (both sides see Connection Timeout)", t)
                return "lost"
            case "vol":
                self.avrcp.delegate.volume = v
                self.avrcp.notify_volume_changed(v)
                log.info("AVRCP: sink volume set to %d at +%gs", v, t)
                return "notified"
        key = PASSTHROUGH[step.action]
        if self.avrcp.avctp_protocol is None:
            log.info("AVRCP key %s at +%gs: waiting up to 5 s for AVRCP", step, t)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.avrcp_ready.wait(), 5)
        if self.avrcp.avctp_protocol is None:
            result = "AVRCP not connected"
            log.warning("AVRCP key %s at +%gs: %s", step, t, result)
            return result
        press = await self.avrcp.passthrough(key, True)
        await asyncio.sleep(0.1)
        release = await self.avrcp.passthrough(key, False)
        result = f"press {press}, release {release}"
        log.info("AVRCP key %s (%s) at +%gs: %s", step, key.name, t, result)
        return result

    async def sink_actions(self, t0: float) -> None:
        """suspend / disconnect: the sink suspends the stream or drops the
        link that many seconds after the stream first started"""
        acts = sorted((t, n) for t, n in ((self.opts.suspend, "suspend"),
                                          (self.opts.disconnect, "disconnect"))
                      if t is not None)
        for t, name in acts:
            await asyncio.sleep(max(0.0, t0 + t - time.monotonic()))
            try:
                if name == "suspend":
                    stream = self.endpoint.stream if self.endpoint else None
                    if stream is None:
                        log.warning("sink: no stream to suspend at +%gs", t)
                        continue
                    log.info("sink: suspends the stream at +%gs", t)
                    await stream.stop()
                elif self.connection is not None:
                    log.info("sink: disconnects at +%gs", t)
                    await self.connection.disconnect()
            except Exception as error:
                log.warning("sink: %s at +%gs failed: %s", name, t, error)

    # -- SDP -------------------------------------------------------------------
    async def query_sdp(self, conn: Connection) -> None:
        """Logs the stack's A2DP source and AVRCP target and controller records"""
        await asyncio.sleep(0.2)
        try:
            async with sdp.Client(conn) as client:
                for uuid in (core.BT_AUDIO_SOURCE_SERVICE,
                             core.BT_AV_REMOTE_CONTROL_TARGET_SERVICE,
                             core.BT_AV_REMOTE_CONTROL_SERVICE):
                    records = await asyncio.wait_for(
                        client.search_attributes([uuid], [(0x0000, 0xFFFF)]), 5)
                    log.info("SDP: %s: %d record(s)", flat(uuid), len(records))
                    for i, record in enumerate(records):
                        for attribute in record:
                            log.info("SDP:   [%d] %s (0x%04X) = %s", i,
                                     sdp.ServiceAttribute.id_name(attribute.id),
                                     attribute.id, flat(attribute.value))
        except Exception as error:
            log.warning("SDP: query failed: %s", flat(error) or repr(error))


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------
def decode(sbc: Path, wav: Path) -> bool:
    """SBC frames to WAV with ffmpeg; its first message logged"""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "sbc", "-i",
           str(sbc), "-f", "wav", str(wav)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        log.info("ffmpeg: %s", error)
        return False
    if r.stderr.strip():
        lines = r.stderr.splitlines()
        log.info("ffmpeg: %d message(s), first: %s", len(lines), lines[0])
    return r.returncode == 0


def report(opts: SinkOptions, rec: Recorder, sink: Sink, tally: Tally) -> bool:
    def out(s: str = "") -> None:
        log.info("%s", s)
    out("=" * 26 + " bt-peer sink report " + "=" * 26)
    out("configuration: " + (str(rec.config) if rec.config else "none was set"))
    for t, name, result in sink.key_results:
        out(f"AVRCP key {name} at +{t:g} s: {result}")
    if opts.hf is not None:
        out(f"HFP: {sink.hf_status}")
        out("HFP battery reports: "
            + (", ".join(f"{v}% {r}" for _t, v, r in sink.hf_reports) or "none"))
    if sink.volume is not None:
        t0 = sink.started_at or 0
        out("absolute volume set by the stack: "
            + (", ".join(f"{v} at {t - t0:+.1f} s" for t, v in sink.volume.sets) or "never"))
    out("controller patches exercised: " + tally.patches_line())
    timing = rec.summary(out)
    wav: WavAnalysis | None = None
    decoded = False
    if rec.packets:
        decoded = decode(rec.path, opts.out / "stream.wav")
        if decoded:
            wav = analyse_wav(opts.out / "stream.wav", out)
            for start, length in wav.gaps[:20]:
                out(f"  GAP: {length:.0f} ms below half the median level at {start:.3f} s")
            if wav.edges:
                out(f"  (ignored {len(wav.edges)} short low run(s) at the very start/end)")
    problems = rec.problems
    reasons = judge(received=bool(rec.packets), problems=problems,
                    audio_seconds=rec.audio_seconds, timing=timing, wav=wav, decoded=decoded,
                    expect_freq=opts.expect_freq, seconds=opts.seconds,
                    violations=tally.violations_line(),
                    hf_status=sink.hf_status if opts.hf is not None else None)
    medians = wav.medians if wav else []
    tone = "/".join(f"{m:.1f}" if m else "-" for m in medians) or "-"
    out(f"RESULT: {'FAIL' if reasons else 'PASS'}  tone {tone} Hz (expect "
        f"{opts.expect_freq:g} +-2%)  audio gaps {len(wav.gaps) if wav else 0}  stalls>100ms "
        f"{len(timing.stalls)}  RTP seq gaps {problems.get('seq', 0)}  SBC errors "
        f"{sum(problems.get(k, 0) for k in ('sbc', 'crc', 'config'))}  stack violations "
        f"{sum(tally.violations.values())}  streamed {rec.audio_seconds:.2f} s (need "
        f"{opts.seconds:g})" + (f"  [{'; '.join(reasons)}]" if reasons else ""))
    return not reasons


# ----------------------------------------------------------------------------
# Run
# ----------------------------------------------------------------------------
async def run(opts: SinkOptions) -> int:
    """The peer until the report (or a signal); 0: PASS"""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    tally = Tally()
    link = LocalLink()
    recorder = Recorder(opts.out / "stream.sbc")
    sink = Sink(opts, link, recorder, tally)
    await sink.device.power_on()
    server = StackHciServer(link, opts.stack, tally, opts.out / "hci-stack.btsnoop")
    sink.stack_server = server
    host, port = split_hci(opts.hci)
    await server.start(host, port)
    log.info('peer ready: stack HCI on %s:%d (controller A %s), sink "%s" %s, mode %s',
             host or "*", server.port, opts.stack.address, SINK_NAME, opts.sink_address,
             ("sink connects" + (" + configures" if opts.configure else ""))
             if opts.connect else "wait for the stack")
    if opts.connect:
        sink.start_connect_loop(opts.connect_delay)
    done = asyncio.ensure_future(recorder.wait_done(opts.seconds))
    stopped = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait([done, stopped], return_when=asyncio.FIRST_COMPLETED)
        log.info("stopping: %s", "signal" if stop.is_set() else f"{opts.seconds:g} s streamed")
        recorder.stop()
        ok = report(opts, recorder, sink, tally)
        if not opts.exit_after and not stop.is_set():
            log.info("report done; still serving (Ctrl-C to exit)")
            await stopped
    finally:
        done.cancel()
        stopped.cancel()
        sink.stopping = True
        server.close()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
    return 0 if ok else 1


def main(opts: SinkOptions) -> int:
    opts.out.mkdir(parents=True, exist_ok=True)
    if opts.forget:
        (opts.out / "keys.json").unlink(missing_ok=True)
    with peer_logging(opts.out / "peer.log", opts.verbose, opts.trace):
        return asyncio.run(run(opts))
