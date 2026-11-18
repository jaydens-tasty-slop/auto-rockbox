"""The Ts28 class: the TS17's transport with the TS28's board, and the unit
check that keeps one Lark player's image out of the other."""

from __future__ import annotations

import importlib
from pathlib import Path
from unittest import mock

from auto_rockbox import registry
from auto_rockbox.keys import KeyScriptError
from auto_rockbox.targets.ts17 import Ts17, Ts17Error, nor
from auto_rockbox.targets.ts28 import Ts28
from auto_rockbox.workspace import Workspace


def test_registered() -> None:
    importlib.import_module("auto_rockbox.commands")
    assert registry.targets()["ts28"] is Ts28
    cmds = registry.commands_for(Ts28)
    for name in ("run", "log", "keys", "battery", "nor dump", "nor program", "reboot", "regs"):
        assert name in cmds, name
    assert "stock-adfu" not in cmds     # stock's USB disk differs (10d6:b00b), not wired yet
    assert (Ts28.lcd.width, Ts28.lcd.height) == (240, 320)


def test_defaults() -> None:
    t = Ts28(Workspace(Path("/w"), {"paths": {"build": "out"}}))
    assert t.image() == Path("/w/out/ts28/rockbox.elf")
    assert t.shell().pid != Ts17(Workspace(Path("/w"))).shell().pid


def test_key_scripts() -> None:
    t = Ts28(Workspace(Path("/nonexistent")))
    for ok in ("9000 B S M V v P+", "BM", "S* V+++"):
        t.check_keys(ok)
    for bad in ("U", "D", "L R", "b"):
        try:
            t.check_keys(bad)
        except KeyScriptError:
            continue
        raise AssertionError(f"{bad!r} passed")


def test_unit_check() -> None:
    """Each Lark target writes only to its own model; a config can name another text"""
    ts17, ts28 = Ts17(Workspace(Path("/w"))), Ts28(Workspace(Path("/w")))
    session = mock.Mock()
    with mock.patch.object(nor, "model_tag", return_value="xlx_TS28"):
        ts28.check_unit(session)
        try:
            ts17.check_unit(session)
        except Ts17Error as e:
            assert "Nothing written" in str(e)
        else:
            raise AssertionError("a TS17 write went to a TS28")
    other = Ts28(Workspace(Path("/w"), {"targets": {"ts28": {"model_tag": "xlx_TS28_B"}}}))
    with mock.patch.object(nor, "model_tag", return_value="xlx_TS28_B"):
        other.check_unit(session)
