"""The flash writer's guards: the boot chain, the rescue stub, image checks."""

from __future__ import annotations

import struct
from collections.abc import Callable
from typing import cast
from unittest import mock

from auto_rockbox.targets.ts17 import nor, rdm
from auto_rockbox.targets.ts17.adfu import Ts17Error
from auto_rockbox.targets.ts17.gadget import Ops, Session

APP_VECTORS = struct.pack("<II", 0x010087F0, 0x10001141)    # a real build's


def _stub(version: int = 1) -> bytes:
    head = struct.pack("<II4sI", 0x01024000, 0x10000011, nor.RESCUE_MARK, version)
    return head.ljust(nor.SECTOR, b"\xff")


def _image(app: bytes = APP_VECTORS) -> bytes:
    return _stub() + app + bytes(0x100)


def _old_image() -> bytes:
    return struct.pack("<II", 0x010087F0, 0x10000141).ljust(0x2000, b"\0")


def _raises(fn: Callable[..., object], *args: object) -> str:
    try:
        fn(*args)
    except Ts17Error as e:
        return str(e)
    raise AssertionError(f"{fn} accepted {args!r:.60}")


def test_check_image() -> None:
    assert nor.check_image(_image(), "new") == 0x10000011
    assert nor.check_image(_old_image(), "old") == 0x10000141
    assert "ELF" in _raises(nor.check_image, b"\x7fELF\x01\x01\x01\0" + bytes(64), "elf")
    for sp, reset in ((0x01000000, 0x10001141), (0x01024004, 0x10001141),
                      (0x010087F0, 0x10001140), (0x010087F0, 0x10000141),
                      (0x010087F0, 0x101A7001)):
        msg = _raises(nor.check_image, _image(struct.pack("<II", sp, reset)), "bad")
        assert "refuse Rockbox's vectors" in msg, msg
    assert nor.check_image(_image(struct.pack("<II", 0x01024000, 0x101A6FFF)), "edge")
    assert "nothing after" in _raises(nor.check_image, _stub(), "stub only")
    # Rockbox alone, for 0x15000 (a release package's image)
    assert nor.check_app(APP_VECTORS + bytes(0x100), "app") == 0x10001141
    assert "ELF" in _raises(nor.check_app, b"\x7fELF\x01\x01\x01\0", "elf")
    assert "refuse" in _raises(nor.check_app, _image(), "a whole image")
    assert "refuse" in _raises(nor.check_app, b"\xff" * 64, "erased")


class _Session:
    """Counts gadget calls; flash reads come from a patched _rom_read"""

    def __init__(self) -> None:
        self.calls = 0

    def values(self, ops: Ops) -> list[int]:
        self.calls += 1
        return [0]


def _check(stored: bytes, want: bytes) -> None:
    s = cast(Session, _Session())
    cells = rdm.encode(want)
    with mock.patch.object(nor, "_rom_read", lambda _s, _a, _n: rdm.encode(stored)):
        nor._check_rescue(s, cells)


def test_rescue_sector_guard() -> None:
    old = _old_image()[:nor.SECTOR]
    stock = bytes(range(256)) * 16
    _check(_stub(), _stub())                  # Rockbox updates leave it alone
    _check(old, stock)                        # no stub either side: as before
    for stored, want in ((_stub(), _stub(2)), (_stub(), stock), (old, _stub())):
        msg = _raises(_check, stored, want)
        assert "--rescue" in msg, msg


def _write(stored: bytes, start: int, want: bytes, *, rescue: bool = False) -> int:
    """write_cells with every sector reading as STORED (cells of WANT equal
    to it are skipped, so a pass writes nothing)"""
    s = cast(Session, _Session())
    with mock.patch.object(nor, "_rom_read", lambda _s, _a, _n: rdm.encode(stored)):
        return nor.write_cells(s, start, rdm.encode(want), rescue=rescue)


def test_write_refuses_before_erasing() -> None:
    fake = _Session()
    s = cast(Session, fake)
    with mock.patch.object(nor, "_rom_read", lambda _s, _a, _n: rdm.encode(_stub())):
        msg = _raises(lambda: nor.write_cells(s, nor.FW0_SYS, rdm.encode(bytes(nor.SECTOR))))
    assert "--rescue" in msg and fake.calls == 1, (msg, fake.calls)   # setup only
    msg = _raises(lambda: nor.write_cells(s, 0x4000, bytes(nor.SECTOR)))
    assert "boot chain" in msg, msg
    # stock's data after fw0_sys (settings, resource files), or reaching into it
    calls = fake.calls
    for start, n in ((nor.FW0_SYS_END, 1), (0x3FF000, 1), (nor.FW0_SYS_END - nor.SECTOR, 2)):
        msg = _raises(lambda a=start, k=n: nor.write_cells(s, a, bytes(k * nor.SECTOR)))
        assert "stock's data" in msg, msg
    assert fake.calls == calls                  # refused before the device


def test_only_the_pinned_stub() -> None:
    # a stub that is not the pinned one, even asked for and even unchanged
    for stored in (_stub(), bytes(nor.SECTOR)):
        msg = _raises(lambda s=stored: _write(s, nor.FW0_SYS, _stub(), rescue=True))
        assert "pinned" in msg, msg


def test_rockbox_alone_needs_a_stub() -> None:
    stock = bytes(range(256)) * 16
    assert "no rescue stub" in _raises(_write, stock, nor.APP, stock)
    assert _write(_stub(), nor.APP, _stub()) == 0     # a stub stored: allowed
