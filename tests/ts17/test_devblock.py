"""Run config and log ring, as dev-ts17.c lays them out."""

from __future__ import annotations

import struct

from auto_rockbox.targets.ts17 import Ts17Error, devblock
from auto_rockbox.targets.ts17.devblock import FLAG_TRACE, FLAG_USB, FLAG_VOLUME, RunOptions


def _raises(fn: object, *args: object, match: str = "") -> None:
    assert callable(fn)
    try:
        fn(*args)
    except Ts17Error as e:
        assert match in str(e), e
        return
    raise AssertionError(f"{fn} did not raise")


def test_magics_spell_their_names() -> None:
    assert struct.pack("<I", devblock.CFG_MAGIC) == b"TSDC"
    assert struct.pack("<I", devblock.LOG_MAGIC) == b"TSLG"
    assert struct.pack("<I", devblock.LAUNCH_TOKEN) == b"LRUN"


def test_run_config_bytes() -> None:
    cfg = devblock.run_config(30, RunOptions(FLAG_USB | FLAG_TRACE | FLAG_VOLUME, -30),
                              "9000 D S")
    assert len(cfg) == 0x100 == devblock.LOG - devblock.CFG
    assert cfg[:16] == b"TSDC" + struct.pack("<IIi", 30, 7, -30)
    assert cfg[16:24] == b"9000 D S"
    assert cfg[24:] == bytes(0xF0 - 8)


def test_run_config_key_limit() -> None:
    assert devblock.run_config(0, RunOptions(), "D" * 239)[16 + 238:] == b"D\0"
    _raises(devblock.run_config, 0, RunOptions(), "D" * 240, match="239")


def test_run_options() -> None:
    assert devblock.run_options({}, usb=False) == RunOptions()
    assert devblock.run_options({}, usb=True) == RunOptions(FLAG_USB)
    assert devblock.run_options({"trace": "1"}, usb=False) == RunOptions(FLAG_TRACE)
    assert devblock.run_options({"trace": "0"}, usb=True) == RunOptions(FLAG_USB)
    assert devblock.run_options({"volume": "-30"}, usb=False) == RunOptions(FLAG_VOLUME, -30)
    _raises(devblock.run_options, {"play": "/a.mp3"}, False, match="trace=0|1")
    _raises(devblock.run_options, {"trace": "yes"}, False, match="0 or 1")
    for bad in ("-73", "1", "-3.5", "loud"):
        _raises(devblock.run_options, {"volume": bad}, False, match="-72..0")


def _ring(lines: bytes, size: int) -> tuple[int, bytes]:
    """What the image's log_line() leaves: total and the ring"""
    ring = bytearray(size)
    for i, b in enumerate(lines):
        ring[i % size] = b
    return len(lines), bytes(ring)


def test_log_unwrapped() -> None:
    total, ring = _ring(b"boot\nkeys: done\n", 64)
    assert devblock.log_text(total, ring) == "boot\nkeys: done\n-- 16 bytes logged\n"


def test_log_wrapped() -> None:
    text = b"".join(b"line %d\n" % i for i in range(10))     # 70 bytes into 32
    total, ring = _ring(text, 32)
    out = devblock.log_text(total, ring)
    assert out == text[-32:].decode() + "-- 70 bytes logged (ring wrapped)\n"


def test_log_exactly_full_and_cut_line() -> None:
    total, ring = _ring(b"0123456789abcdef", 16)
    assert devblock.log_text(total, ring) == "0123456789abcdef\n-- 16 bytes logged\n"


def test_log_header() -> None:
    good = struct.pack("<III", devblock.LOG_MAGIC, 70, 3808)
    assert devblock.ring_size(good) == (70, 3808)
    _raises(devblock.ring_size, devblock.EMPTY_LOG_HEADER, match="no dev log")
    _raises(devblock.ring_size, struct.pack("<III", devblock.LOG_MAGIC, 1, 0x2000))
