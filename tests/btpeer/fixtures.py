"""SBC frames and RTP media packets for the tests: silent frames (zero scale
factors and samples) with a correct header CRC, a tone encoded by ffmpeg,
and packets as an A2DP source sends them. No Bumble here."""

from __future__ import annotations

import logging
import struct
import subprocess
from pathlib import Path

from auto_rockbox.btpeer import sbc

# 44.1 kHz joint stereo, 16 blocks, 8 subbands, loudness, bitpool 35: what
# Rockbox and most phones send
JOINT = sbc.Frame(rate=44100, blocks=16, mode=3, alloc=0, subbands=8, bitpool=35)
SSRC = 0x1234

# The media checks log their warnings to "peer": not to stderr in the tests
logging.getLogger("peer").addHandler(logging.NullHandler())


def frame(f: sbc.Frame = JOINT) -> bytes:
    """A silent frame with header F and its CRC"""
    b1 = (sbc.RATES.index(f.rate) << 6 | (f.blocks // 4 - 1) << 4 | f.mode << 2
          | f.alloc << 1 | (f.subbands == 8))
    data = bytearray(f.length)
    data[:3] = bytes([sbc.SYNC, b1, f.bitpool])
    for crc in range(256):
        data[3] = crc
        if sbc.crc_ok(bytes(data), f):
            return bytes(data)
    raise AssertionError("no CRC fits")


def rtp(seq: int, ts: int, payload: bytes, ssrc: int = SSRC) -> bytes:
    return bytes([0x80, 96]) + struct.pack(">HII", seq & 0xFFFF, ts, ssrc) + payload


def packet(seq: int, ts: int, frames: int = 5, f: sbc.Frame = JOINT) -> bytes:
    """An RTP packet of FRAMES whole frames (SBC payload header: their count)"""
    return rtp(seq, ts, bytes([frames]) + frame(f) * frames)


def stream(n: int, frames: int = 5, f: sbc.Frame = JOINT) -> list[bytes]:
    """N packets in sequence, stamped in samples"""
    return [packet(i, i * frames * f.samples, frames, f) for i in range(n)]


def make_tone(path: Path, seconds: float, hz: float = 1000.0) -> None:
    """SBC frames of a stereo tone (ffmpeg's encoder, joint stereo, bitpool
    about 35)"""
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", f"sine=frequency={hz:g}:duration={seconds:g}:sample_rate=44100",
                    "-ac", "2", "-c:a", "sbc", "-b:a", "229k", "-f", "sbc", str(path)],
                   check=True, capture_output=True, timeout=60)


def split_frames(data: bytes) -> tuple[sbc.Frame, list[bytes]]:
    """The first frame's header and every frame of DATA"""
    first = sbc.frame_header(data)
    assert isinstance(first, sbc.Frame), first
    frames, off = [], 0
    while off < len(data):
        f = sbc.frame_header(data, off)
        assert isinstance(f, sbc.Frame), f
        frames.append(data[off:off + f.length])
        off += f.length
    return first, frames
