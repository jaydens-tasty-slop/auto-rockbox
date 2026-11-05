"""What the Bumble peers share: a classic-only device with its link keys in
a file, the A2DP sink's codec capabilities and SDP records, authentication
that pairs again when the stack has lost its key, and the hook that lets a
peer log or drop a protocol message inside Bumble."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from bumble import a2dp, avdtp, avrcp, hci, sdp
from bumble.device import Connection, Device, DeviceConfiguration
from bumble.host import Host
from bumble.keys import JsonKeyStore
from bumble.pairing import PairingConfig, PairingDelegate

from .logs import log
from .sbc import MAX_BITPOOL, Element

E = hci.HCI_ErrorCode
HEADPHONES_COD = 0x240404           # audio/video, wearable headset
SBC = a2dp.SbcMediaCodecInformation


def intercept(obj: object, name: str, hook: Callable[[Callable[..., Any]], Callable[..., Any]]
              ) -> None:
    """Replaces method NAME of OBJ (a Bumble object the peer does not make
    itself) with HOOK(the original): how a peer logs a protocol's messages or
    leaves one unanswered"""
    setattr(obj, name, hook(getattr(obj, name)))


def classic_device(name: str, cod: int, host: Host, keys: Path, delegate: PairingDelegate, *,
                   address: str | None = None, mitm: bool = False, ssp: bool = True,
                   cls: type[Device] = Device) -> Device:
    """A BR/EDR-only device (no Secure Connections, no SMP) that bonds and
    keeps its link keys in KEYS; ADDRESS: its public address on a virtual
    controller (an adapter has its own)"""
    config = DeviceConfiguration(name=name, class_of_device=cod, classic_enabled=True,
                                 le_enabled=False, classic_sc_enabled=False,
                                 classic_smp_enabled=False, classic_ssp_enabled=ssp)
    if address:
        config.address = hci.Address(address, hci.Address.PUBLIC_DEVICE_ADDRESS)
    device = cls(config=config, host=host)
    device.keystore = JsonKeyStore(None, str(keys))
    device.pairing_config_factory = lambda _c: PairingConfig(
        sc=False, mitm=mitm, bonding=True, delegate=delegate)
    return device


def just_works() -> PairingDelegate:
    return PairingDelegate(io_capability=PairingDelegate.NO_OUTPUT_NO_INPUT)


async def authenticate(device: Device, conn: Connection) -> None:
    """Authenticates CONN; if the other side rejects the stored key (it
    forgot the pairing), deletes it and pairs again"""
    try:
        await conn.authenticate()
    except hci.HCI_Error as error:
        if error.error_code not in (E.PIN_OR_KEY_MISSING_ERROR,
                                    E.AUTHENTICATION_FAILURE_ERROR):
            raise
        log.warning("security: stored key rejected (%s); deleting it and pairing again",
                    hci.HCI_Constant.error_name(error.error_code))
        if device.keystore is not None:
            with contextlib.suppress(KeyError):
                await device.keystore.delete(str(conn.peer_address))
        await conn.authenticate()


def sink_capabilities() -> avdtp.MediaCodecCapabilities:
    """An SBC sink that takes every mode, 44.1 or 48 kHz, bitpool 2..53"""
    return avdtp.MediaCodecCapabilities(
        media_type=avdtp.AVDTP_AUDIO_MEDIA_TYPE, media_codec_type=a2dp.A2DP_SBC_CODEC_TYPE,
        media_codec_information=SBC(
            sampling_frequency=SBC.SamplingFrequency.SF_44100 | SBC.SamplingFrequency.SF_48000,
            channel_mode=(SBC.ChannelMode.MONO | SBC.ChannelMode.DUAL_CHANNEL
                          | SBC.ChannelMode.STEREO | SBC.ChannelMode.JOINT_STEREO),
            block_length=(SBC.BlockLength.BL_4 | SBC.BlockLength.BL_8
                          | SBC.BlockLength.BL_12 | SBC.BlockLength.BL_16),
            subbands=SBC.Subbands.S_4 | SBC.Subbands.S_8,
            allocation_method=SBC.AllocationMethod.LOUDNESS | SBC.AllocationMethod.SNR,
            minimum_bitpool_value=2, maximum_bitpool_value=MAX_BITPOOL))


def sbc_element(info: SBC) -> Element:
    return Element(int(info.sampling_frequency), int(info.channel_mode),
                   int(info.block_length), int(info.subbands), int(info.allocation_method),
                   info.minimum_bitpool_value, info.maximum_bitpool_value)


def sink_records(volume_target: bool) -> dict[int, list[sdp.ServiceAttribute]]:
    """SDP: an A2DP sink and AVRCP controller; VOLUME_TARGET: an AVRCP
    target too (absolute volume)"""
    records = {
        0x00010001: a2dp.make_audio_sink_service_sdp_records(0x00010001),
        0x00010002: avrcp.ControllerServiceSdpRecord(0x00010002).to_service_attributes(),
    }
    if volume_target:
        records[0x00010003] = avrcp.TargetServiceSdpRecord(0x00010003).to_service_attributes()
    return records
