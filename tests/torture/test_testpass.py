"""`arb TARGET test` offline: areas, the order of a pass, what is left out
and why, skips, and a pass through the engine against the simulated player
(tests/torture/fakes.py), crashes and all."""

from __future__ import annotations

import tempfile
from argparse import Namespace
from pathlib import Path
from unittest import mock

from auto_rockbox import engine
from auto_rockbox.registry import Context
from auto_rockbox.suites import rockbox_torture as rt
from auto_rockbox.suites import testpass
from auto_rockbox.workspace import Workspace

from .fakes import Clock, FakePlayer, FakeTorture, SimPlayer, SimSuite, fast

ORDER = ["sim-ok", "sim-fail", "sim-glitch", "sim-crash", "sim-poweroff", "sim-hang",
         "sim-leak", "sim-skip"]        # storage, audio, power, usb, playback, ui


def test_areas() -> None:
    """Every scenario has one of the areas; a typo fails at registration"""
    assert all(s.area in engine.AREAS for s in engine._scenarios)
    try:
        engine.scenario("x", suite="torture", area="dac")
    except ValueError as e:
        assert "area 'dac'" in str(e)
    else:
        raise AssertionError("an unknown area registered")


def test_plan() -> None:
    """A pass runs the scenarios in area order, a weight-0 one only when
    named; those missing a need are listed with it"""
    ws = Workspace(Path("/nonexistent"))
    s = SimSuite(SimPlayer(ws), Path("x.elf"))
    p = testpass.plan(s, set(), set(), 1)
    assert p.names == ORDER
    assert p.left_out == {"sim-off": "off by default (--only sim-off)"}
    assert testpass.plan(s, {"audio", "usb"}, set(), 2).names == \
        ["sim-fail", "sim-fail", "sim-glitch", "sim-glitch", "sim-hang", "sim-hang"]
    p = testpass.plan(s, {"storage"}, {"sim-off", "sim-ok"}, 1)
    assert (p.names, p.left_out) == (["sim-ok", "sim-off"], {})
    left = engine.left_out(FakeTorture(FakePlayer(ws), Path("x.elf")))
    assert left["sleep"] == f"needs {rt.LCD_GATE}"
    assert left["bt-onoff"] == f"needs {rt.BT_PC}"


def test_pass() -> None:
    """Each step once in order; a crash and a hang are collected and the
    pass goes on; a skip passes but is counted apart; the summary"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        image = root / "rockbox.elf"
        image.write_bytes(b"")
        player = SimPlayer(Workspace(root))
        args = Namespace(image=image, out=root / "pass", areas=[], only=None, repeat=1, seed=1,
                         settings=[], shots=False, after_boot=[], no_boot=False)
        with fast(Clock()), mock.patch.object(testpass, "suite_class", return_value=SimSuite):
            rc = testpass.test_cmd(Context(player.ws, player), args)
        assert rc == 1
        steps = engine.load_steps(root / "pass/torture")
        assert [x["name"] for x in steps] == ORDER
        verdicts = {x["name"]: "skip" if x["skipped"] else "ok" if x["ok"] else x["problems"][0]
                    for x in steps}
        assert verdicts == {"sim-ok": "ok", "sim-fail": "a problem of its own",
                            "sim-glitch": "underruns +1", "sim-crash": "LOST",
                            "sim-poweroff": "ok", "sim-hang": "LOST", "sim-leak": "ok",
                            "sim-skip": "skip"}
        text = (root / "pass/summary.md").read_text()
    assert "| storage | sim-ok | torture | ok |" in text
    assert "| ui | sim-skip | torture | skip | 0 | skipped: nothing to test |" in text
    assert "3 passed, 4 failed, 1 skipped (nothing to test), 1 not run" in text
    assert "- sim-off (torture): off by default (--only sim-off)" in text
