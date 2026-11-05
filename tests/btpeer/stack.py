"""A Bumble host as the stack under test: it connects to the peer's H4
server like the simulator does, pages the peer, pairs and encrypts, then
streams SBC to the virtual sink as an A2DP source, or opens the virtual HID
device's channels and keeps its input reports. Only the tests use it; it
needs Bumble."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from bumble import a2dp, avdtp, hci, hid
from bumble.core import PhysicalTransport
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.host import Host
from bumble.pairing import PairingConfig
from bumble.rtp import MediaPacket
from bumble.transport import open_transport

from auto_rockbox.btpeer import sbc
from auto_rockbox.btpeer.profiles import SBC, just_works

from .fixtures import split_frames

FRAMES_PER_PACKET = 5


def configuration(f: sbc.Frame) -> avdtp.MediaCodecCapabilities:
    """The one configuration the source offers: that of its frames"""
    return avdtp.MediaCodecCapabilities(
        media_type=avdtp.AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=a2dp.A2DP_SBC_CODEC_TYPE,
        media_codec_information=SBC(
            sampling_frequency=SBC.SamplingFrequency(8 >> sbc.RATES.index(f.rate)),
            channel_mode=SBC.ChannelMode(8 >> f.mode),
            block_length=SBC.BlockLength(8 >> (f.blocks // 4 - 1)),
            subbands=SBC.Subbands(1 if f.subbands == 8 else 2),
            allocation_method=SBC.AllocationMethod(1 << f.alloc),
            minimum_bitpool_value=2, maximum_bitpool_value=sbc.MAX_BITPOOL))


class _Packet(MediaPacket):
    def __init__(self, seq: int, samples: int, payload: bytes, rate: int) -> None:
        super().__init__(2, 0, 0, 0, seq, samples, 1, [], 96, payload)
        self.timestamp_seconds = samples / rate


async def _packets(first: sbc.Frame, frames: list[bytes]) -> AsyncGenerator[MediaPacket]:
    """RTP packets of FRAMES_PER_PACKET frames, stamped in samples as A2DP wants"""
    for seq, i in enumerate(range(0, len(frames), FRAMES_PER_PACKET)):
        group = frames[i:i + FRAMES_PER_PACKET]
        yield _Packet(seq, i * first.samples, bytes([len(group)]) + b"".join(group), first.rate)


@asynccontextmanager
async def _stack(port: int, peer: str) -> AsyncGenerator[tuple[Device, Connection]]:
    """The stack on the peer's HCI server on PORT, with a secure link to PEER"""
    async with await open_transport(f"tcp-client:127.0.0.1:{port}") as t:
        config = DeviceConfiguration(name="Stack", classic_enabled=True, le_enabled=False,
                                     classic_sc_enabled=False, classic_smp_enabled=False)
        device = Device(config=config, host=Host(t.source, t.sink))
        delegate = just_works()
        device.pairing_config_factory = lambda _c: PairingConfig(
            sc=False, mitm=False, bonding=False, delegate=delegate)
        await device.power_on()
        conn = await device.connect(hci.Address(peer, hci.Address.PUBLIC_DEVICE_ADDRESS),
                                    transport=PhysicalTransport.BR_EDR)
        await conn.authenticate()
        await conn.encrypt()
        yield device, conn


async def stream_to(port: int, sink_address: str, sbc_file: Path) -> None:
    """Streams SBC_FILE to the sink at SINK_ADDRESS, the way the stack under
    test does"""
    first, frames = split_frames(sbc_file.read_bytes())
    async with _stack(port, sink_address) as (_device, conn):
        protocol = await avdtp.Protocol.connect(conn)
        pump = avdtp.MediaPacketPump(_packets(first, frames))
        source = protocol.add_source(configuration(first), pump)
        await protocol.discover_remote_endpoints()
        remote = protocol.find_remote_sink_by_codec(avdtp.AVDTP_AUDIO_MEDIA_TYPE,
                                                    a2dp.A2DP_SBC_CODEC_TYPE)
        assert remote is not None, "the peer offers no SBC sink"
        stream = await protocol.create_stream(source, remote)
        await stream.start()
        await pump.wait_for_completion()


async def hid_host(port: int, device_address: str, reports: list[bytes]) -> None:
    """Opens the HID device's control and interrupt channels, as the
    stack's HID host does, and keeps what arrives on the interrupt channel
    in REPORTS until cancelled"""
    async with _stack(port, device_address) as (device, conn):
        host = hid.Host(device)
        host.on(hid.HID.EVENT_INTERRUPT_DATA, reports.append)
        host.on_device_connection(conn)             # made after the connection
        await host.connect_control_channel()
        await host.connect_interrupt_channel()
        await asyncio.Event().wait()
