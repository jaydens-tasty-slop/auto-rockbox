"""The Ts17 class against the core: registration, commands, keys, framebuffer."""

from __future__ import annotations

import importlib
import struct
from pathlib import Path
from unittest import mock

from auto_rockbox import fb, registry
from auto_rockbox.elf import Elf, Symbolizer
from auto_rockbox.keys import KeyScriptError
from auto_rockbox.targets.ts17 import FB_SYMBOL, Ts17, adfu, devblock
from auto_rockbox.workspace import Workspace

from . import Skip

BUILD_ELF = Path(__file__).resolve().parents[3] / "build" / "ts17" / "rockbox.elf"


def test_registered_with_commands() -> None:
    importlib.import_module("auto_rockbox.commands")
    assert registry.targets()["ts17"] is Ts17
    cmds = registry.commands_for(Ts17)
    for name in ("run", "log", "fb", "keys", "battery", "card-push", "nor dump", "reboot", "mem",
                 "stock-adfu"):
        assert name in cmds, name
    assert "sh" not in cmds                 # no dev shell on this player
    assert cmds["nor dump"].targets == frozenset({"ts17"})


def test_key_scripts() -> None:
    t = Ts17(Workspace(Path("/nonexistent")))
    for ok in ("9000 D D S 1500 P++", "7000,S,1500,UL", "V v Vv P+++", "D" * 239, ""):
        t.check_keys(ok)
    for bad in ("X", "D*", "S~", "d", "D+x", "D" * 240):
        try:
            t.check_keys(bad)
        except KeyScriptError:
            continue
        raise AssertionError(f"{bad!r} passed")


def test_config_defaults() -> None:
    ws = Workspace(Path("/w"), {"paths": {"build": "out"},
                                "targets": {"ts17": {"card_uuid": "0000-0000"}}})
    t = Ts17(ws)
    assert t.image() == Path("/w/out/ts17/rockbox.elf")
    assert t._card_uuid() == "0000-0000"
    t = Ts17(Workspace(Path("/w"), {"targets": {"ts17": {"image": "x.elf"}}}))
    assert t.image() == Path("/w/x.elf") and t._card_uuid() is None


def test_framebuffer_decode() -> None:
    w, h = Ts17.lcd.width, Ts17.lcd.height
    px = [0] * (w * h)
    px[5] = 0xF800                  # row 0, x 5: red
    px[7 * w] = 0x001F              # row 7, x 0: blue
    px[w * h - 1] = 0x07E0          # last pixel: green
    im = fb.decode(struct.pack(f"<{w * h}H", *px), Ts17.lcd)
    assert im.size == (128, 160)
    assert im.getpixel((5, 0)) == (255, 0, 0)
    assert im.getpixel((0, 7)) == (0, 0, 255)
    assert im.getpixel((127, 159)) == (0, 255, 0)
    assert im.getpixel((1, 1)) == (0, 0, 0)


def test_framebuffer_symbol_in_build() -> None:
    if not BUILD_ELF.exists():
        raise Skip(f"no {BUILD_ELF}")
    assert Elf(BUILD_ELF).address(FB_SYMBOL) == 0x01040000


class _Link:
    """adfu.Adfu over a dev block: the log ring and the last tick's record"""

    def __init__(self, log: bytes, crumb: bytes) -> None:
        self.mem = {devblock.LOG: struct.pack("<III", devblock.LOG_MAGIC, len(log), 64),
                    devblock.LOG + devblock.LOG_HEADER: log.ljust(64, b"\0"),
                    devblock.CRUMB: crumb}

    def __enter__(self) -> _Link:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def read(self, addr: int, n: int) -> bytes:
        return self.mem[addr][:n]


def test_read_log_after_a_hang() -> None:
    crumb = struct.pack("<IIIII", devblock.CRUMB_MAGIC, 4712, 0x10002344, 0x10001001, 0)
    t = Ts17(Workspace(Path("/nonexistent")))
    names = mock.patch.object(Symbolizer, "names", lambda _s, a: [f"f{x:x}" for x in a])
    with names, mock.patch.object(adfu, "Adfu", lambda: _Link(b"track: /a.ac3\n", crumb)):
        log = t.read_log(Path("x.elf"))
    assert log.endswith("last tick 4712: pc 10002344 f10002344, lr 10001001 f10001000\n"), log
    done = f"{devblock.RETURN_LINE} 5000\n".encode()
    with names, mock.patch.object(adfu, "Adfu", lambda: _Link(done, crumb)):
        assert "last tick" not in t.read_log(Path("x.elf"))      # it returned: no hang
