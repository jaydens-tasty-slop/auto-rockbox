"""SBC frame headers, their CRC (on ffmpeg's frames when it is installed),
and the configuration checks of the strict sink."""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
from pathlib import Path

from auto_rockbox.btpeer import sbc
from tests import Skip

from .fixtures import JOINT, frame, make_tone, split_frames

# The codec element's flags: 44.1 kHz, joint stereo, 16 blocks, 8 subbands, loudness
GOOD = sbc.Element(sampling_frequency=2, channel_mode=1, block_length=1, subbands=1,
                   allocation_method=1, min_bitpool=2, max_bitpool=53)


def test_header_round_trip() -> None:
    data = frame(JOINT)
    assert len(data) == JOINT.length == 4 + 8 + (8 + 16 * 35 + 7) // 8
    assert sbc.frame_header(data) == JOINT
    assert JOINT.samples == 128 and JOINT.channels == 2


def test_header_problems() -> None:
    assert sbc.frame_header(b"\x9c\xbd") == "short SBC frame (2 bytes)"
    assert sbc.frame_header(b"\x00\xbd\x23\x00") == "bad SBC sync byte 0x00"


def test_crc_catches_a_flipped_scale_factor() -> None:
    data = bytearray(frame(JOINT))
    assert sbc.crc_ok(bytes(data), JOINT)
    data[5] ^= 0x10                     # a scale factor bit
    assert not sbc.crc_ok(bytes(data), JOINT)
    mono = sbc.Frame(rate=48000, blocks=8, mode=0, alloc=1, subbands=4, bitpool=20)
    assert sbc.crc_ok(frame(mono), mono)


def test_crc_of_ffmpeg_frames() -> None:
    if shutil.which("ffmpeg") is None:
        raise Skip("no ffmpeg")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "tone.sbc"
        make_tone(path, 0.5)
        first, frames = split_frames(path.read_bytes())
    assert first.rate == 44100 and first.channels == 2
    assert len(frames) > 100
    assert all(sbc.crc_ok(f, first) for f in frames)


def test_config_from_element() -> None:
    config = GOOD.config()
    assert config == sbc.Config(44100, 3, 16, 8, 0, 2, 53)
    assert config.frame(35) == JOINT
    assert config.mismatches(JOINT) == []
    assert str(config) == ("SBC 44100 Hz JOINT_STEREO (2 ch), 16 blocks, 8 subbands, "
                           "LOUDNESS, bitpool 2..53 (119-byte frames, 328 kbit/s at max "
                           "bitpool)")
    stereo = sbc.Frame(44100, 16, 2, 0, 8, 60)
    assert config.mismatches(stereo) == ["mode", "bitpool 60 outside 2..53"]


def test_element_problems() -> None:
    def problem(**changes: int) -> int | None:
        p = dataclasses.replace(GOOD, **changes).problem()
        return p[0] if p else None

    assert GOOD.problem() is None
    assert problem(sampling_frequency=3) == 0xC3        # 44.1 and 48 kHz
    assert problem(sampling_frequency=8) == 0xC4        # 16 kHz
    assert problem(channel_mode=0) == 0xC5
    assert problem(block_length=3) == 0xDD
    assert problem(subbands=3) == 0xC7
    assert problem(allocation_method=3) == 0xC9
    assert problem(min_bitpool=1) == 0xCB
    assert problem(min_bitpool=40, max_bitpool=30) == 0xCB
    assert problem(max_bitpool=251) == 0xCD
    assert problem(max_bitpool=54) == 0xCE
    assert sbc.Element(3, 1, 1, 1, 1, 2, 53).config().frame(35) is None  # two rates
