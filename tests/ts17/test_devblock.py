"""Run config and log ring, as dev-ts17.c lays them out."""

from __future__ import annotations

import struct

from auto_rockbox.targets.ts17 import Ts17Error, devblock


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


def test_layout_matches_dev_ts17() -> None:
    assert devblock.LOG - devblock.CFG == 0x200 == 4 + devblock.CFG_TEXT
    assert devblock.RING_BYTES == 0x1000 - 0x200 - 16 - 20


def test_run_config_bytes() -> None:
    cfg = devblock.run_config({"return": "30", "keys": "9000 D S", "usb": "1"})
    assert len(cfg) == devblock.LOG - devblock.CFG
    text = b"return=30\nkeys=9000 D S\nusb=1\n"
    assert cfg[:4] == b"TSDC"
    assert cfg[4:4 + len(text)] == text
    assert cfg[4 + len(text):] == bytes(devblock.CFG_TEXT - len(text))


def test_run_config_limit() -> None:
    fits = devblock.CFG_TEXT - 1 - len("keys=\n")
    assert devblock.run_config({"keys": "D" * fits})[-2:] == b"\n\0"   # the NUL stays
    _raises(devblock.run_config, {"keys": "D" * (fits + 1)}, match="run config over")


def test_check_settings() -> None:
    for ok in ({}, {"trace": "1"}, {"volume": "-30"}, {"volume": "0"}, {"bluetooth": "off"},
               {"bluetooth": "card"}, {"usb": "storage"}, {"play": "/a.mp3"}):
        devblock.check_settings(ok)
    _raises(devblock.check_settings, {"bluetooth": "on"}, match="off|card")
    _raises(devblock.check_settings, {"speaker": "0"}, match="no such run setting")
    _raises(devblock.check_settings, {"trace": "yes"}, match="0|1")
    for bad in ("-73", "1", "-3.5", "loud"):
        _raises(devblock.check_settings, {"volume": bad}, match="-72..0")


def test_crumb() -> None:
    assert devblock.CRUMB + devblock.CRUMB_SIZE == devblock.DEV_BLOCK + devblock.DEV_BLOCK_SIZE
    raw = struct.pack("<IIIII", devblock.CRUMB_MAGIC, 4712, 0x1000_2345, 0x1000_1001, 1)
    assert devblock.crumb(raw) == devblock.Crumb(4712, 0x1000_2345, 0x1000_1001, True)
    assert devblock.crumb(bytes(devblock.CRUMB_SIZE)) is None      # cleared at launch


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
    good = struct.pack("<III", devblock.LOG_MAGIC, 70, devblock.RING_BYTES)
    assert devblock.ring_size(good) == (70, devblock.RING_BYTES)
    _raises(devblock.ring_size, devblock.EMPTY_LOG_HEADER, match="no dev log")
    _raises(devblock.ring_size, struct.pack("<III", devblock.LOG_MAGIC, 1, 0x2000))
    _raises(devblock.ring_size, struct.pack("<III", devblock.LOG_MAGIC, 1, 3804))  # 0x100 layout
