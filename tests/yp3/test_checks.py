"""`arb yp3 checks` against a scripted dev shell and person: what each step
sends and asks, how it judges, waiting for the shell, and the boot and
return around the steps (the target's device calls replaced). No device is
touched.

    .venv/bin/python -m tests yp3
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from collections.abc import Callable
from pathlib import Path

from auto_rockbox import cli, registry
from auto_rockbox.shell import NoShell, Reply
from auto_rockbox.target import RunRequest
from auto_rockbox.targets.yp3 import Yp3, Yp3Run
from auto_rockbox.targets.yp3.checks import STEPS, Checks, booted
from auto_rockbox.targets.yp3.rom import DeviceError, UsbId
from auto_rockbox.workspace import Workspace

REPLIES = {
    "keylog 20": ["keylog: buttons now 0; press keys now", "buttons 1 (adc 10)",
                  "buttons 0 (adc 900)", "buttons 2 (adc 300)", "buttons c (adc 500)",
                  "buttons 0 (adc 900)", "buttons 10 (adc 900)"],
    "rec 6 mic mic": ["rec /yp3dev/mic.wav: status 1", "rec: 6000 ms recorded"],
    "fm 97100": ["fm present=1 tuned=1 stereo=1 rssi=40"],
    "rec 5": ["rec /yp3dev/rec.wav: status 1", "rec: 5000 ms recorded"],
    "date": ["2026-11-05 10:00:00 wday 4"],
}


class Player:
    """The dev shell: canned replies, each line passed to the echo as it
    comes; the first DOWN calls find no port"""

    def __init__(self, replies: dict[str, list[str]], down: int = 0,
                 fail: frozenset[str] = frozenset()) -> None:
        self.replies, self.down, self.fail = replies, down, fail
        self.sent: list[str] = []

    def __call__(self, line: str, timeout: float, echo: Callable[[str], None] | None) -> Reply:
        if self.down:
            self.down -= 1
            raise NoShell("no serial port")
        self.sent.append(line)
        ok = line not in self.fail
        lines = [*self.replies.get(line, []), "ok" if ok else "err"]
        for x in lines:
            if echo:
                echo(x)
        return Reply(ok, lines)


class Bench:
    """A person answering in turn, a clock that sleeps instantly, and the output"""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.asked: list[str] = []
        self.out: list[str] = []
        self.t = 0.0

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.answers.pop(0)

    def sleep(self, s: float) -> None:
        self.t += s

    def checks(self, player: Player) -> Checks:
        return Checks(player, 97100, self.ask, self.out.append, self.sleep, lambda: self.t,
                      lambda: "HOST")


def test_all_steps() -> None:
    player = Player(REPLIES)
    bench = Bench(["",                      # keys: Enter
                   "", "", "n",             # mic: Enter, headphones in, heard nothing
                   "", "yes",               # fm: Enter, a station
                   "", "Y",                 # speaker
                   "", "y", ""])            # poweroff: off, on again
    results = bench.checks(player).run(STEPS)
    assert results == {"keys": True, "mic": False, "fm": True, "speaker": True,
                       "poweroff": True}, results
    assert bench.answers == []
    assert player.sent == ["keylog 20", "rec 6 mic mic", "vol -20",
                           "play /yp3dev/mic.wav", "stop", "vol -20", "fm 97100", "rec 5",
                           "fm off", "amp 1", "vol -20", "play /yp3dev/tone.mp3", "amp 0",
                           "stop", "uptime", "date"], player.sent
    out = bench.out
    assert "    pressed: LEFT+RIGHT" in out and "    pressed: (released)" in out
    assert "    seen: CENTRE, DOWN, LEFT, RIGHT, UP | missing: none" in out
    assert "    rec: 6000 ms recorded" in out
    assert "    fm present=1 tuned=1 stereo=1 rssi=40" in out
    assert "    player: 2026-11-05 10:00:00 wday 4  host: HOST" in out
    assert bench.asked[0].startswith("\n>>> Keys") and bench.asked[3].endswith("[y/n] ")


def test_keys_missing() -> None:
    player = Player({"keylog 20": ["buttons 1 (adc 10)", "buttons 0 (adc 900)"]})
    bench = Bench([""])
    assert bench.checks(player).run(["keys"]) == {"keys": False}
    assert bench.out[-1] == "    seen: UP | missing: DOWN, LEFT, RIGHT, CENTRE", bench.out


def test_mic_not_recorded() -> None:
    player = Player({"rec 6 mic mic": ["rec: busy (database update)"]},
                    fail=frozenset({"rec 6 mic mic"}))
    bench = Bench([""])
    assert bench.checks(player).run(["mic"]) == {"mic": False}
    assert player.sent == ["rec 6 mic mic"]                 # nothing to play back
    assert bench.out == ["    rec: busy (database update)", "    err"], bench.out


def test_poweroff_without_shell() -> None:
    bench = Bench(["", "n", ""])
    checks = bench.checks(Player(REPLIES, down=1000))
    assert checks.run(["poweroff"]) == {"poweroff": False}
    assert bench.t == 60.0                                  # waited the minute, in 2 s steps
    assert bench.out[-1].startswith("    no serial shell"), bench.out


def test_wait_shell() -> None:
    bench = Bench([])
    player = Player(REPLIES, down=3)
    assert bench.checks(player).wait_shell(10.0)
    assert (bench.t, player.sent) == (6.0, ["uptime"])
    bench = Bench([])
    assert not bench.checks(Player(REPLIES, down=1)).wait_shell(0)     # one try
    assert bench.t == 0.0
    assert not bench.checks(Player(REPLIES, fail=frozenset({"uptime"}))).wait_shell(0)


class FakeYp3(Yp3):
    """The YP3 with its device calls recorded instead"""

    def __init__(self, ws: Workspace, rom_fails: bool = False) -> None:
        super().__init__(ws)
        self.calls: list[str] = []
        self.rom_fails = rom_fails

    def to_rom(self, timeout: float = 120) -> UsbId:
        self.calls.append(f"to_rom {timeout:g}")
        if self.rom_fails and len(self.calls) > 1:
            raise DeviceError("the player did not come to ROM USB in 60 s")
        return UsbId("1-1", 1, 2)

    def before_run(self, req: RunRequest) -> None:
        self.calls.append("before_run")

    def launch(self, req: RunRequest) -> None:
        assert isinstance(req, Yp3Run) and req.usb and req.seconds == 1500
        self.calls.append(f"launch {req.image.name}")

    def after_run(self, req: RunRequest, returned: bool) -> None:
        self.calls.append(f"after_run {returned}")


def _target(d: str, rom_fails: bool = False) -> FakeYp3:
    # a lock file of its own: the real one may be held by a run on the bench
    return FakeYp3(Workspace(Path(d), {"targets": {"yp3": {"lock": "arb-test-checks.lock"}}}),
                   rom_fails)


def test_booted() -> None:
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
        t = _target(d)
        bench = Bench([""])
        player = Player(REPLIES, down=2)
        results = booted(t, bench.checks(player), ["keys"], Path(d, "rockbox.elf"), 1500)
    assert results == {"keys": True}
    assert t.calls == ["to_rom 120", "before_run", "launch rockbox.elf", "to_rom 60",
                       "after_run True"], t.calls
    assert player.sent == ["uptime", "keylog 20"]


def test_booted_without_shell() -> None:
    with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(io.StringIO()):
        t = _target(d, rom_fails=True)
        bench = Bench([])
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                booted(t, bench.checks(Player(REPLIES, down=10 ** 6)), ["keys"],
                       Path(d, "rockbox.elf"), 1500)
        except NoShell as e:
            assert "no serial shell after the boot" in str(e)
        else:
            raise AssertionError("no NoShell")
    assert t.calls == ["to_rom 120", "before_run", "launch rockbox.elf", "to_rom 60"], t.calls
    assert "with the charger unguarded" in err.getvalue(), err.getvalue()
    assert bench.t == 150.0


def test_command_line() -> None:
    ws = Workspace(Path("/nonexistent"))
    registry.load_plugins(ws)
    args = cli.parser(ws).parse_args(["yp3", "checks", "keys", "fm", "--fm-khz", "99000"])
    assert (args.steps, args.fm_khz, args.seconds) == (["keys", "fm"], 99000, 1500)
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            cli.parser(ws).parse_args(["yp3", "checks", "radio"])
        except SystemExit as e:
            assert e.code == 2
        else:
            raise AssertionError("radio taken as a step")
