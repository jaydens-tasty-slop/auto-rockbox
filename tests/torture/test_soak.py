"""Every player's soak scenarios (suites/soak.py) offline, on a player whose
image is back at once with a log made from the run's settings: which
players get them, what each boot asks for, and how its log is judged."""

from __future__ import annotations

import random
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from unittest import mock

from auto_rockbox import card, engine
from auto_rockbox.engine import Outcome
from auto_rockbox.lock import DeviceLock
from auto_rockbox.suites import soak
from auto_rockbox.suites.soak import SoakSuite
from auto_rockbox.target import Lcd, RunRequest, Target
from auto_rockbox.targets.ts17 import Ts17
from auto_rockbox.workspace import Workspace

RETURN = "dev return: on time"
GENERIC = {"boot", "crash", "poweroff", "sdtest", "fsck", "tone"}


class LogPlayer(Target):
    """A player whose image is back at once; its log is LOG(request)"""

    name = "logfake"
    summary = "the soak tests' player"
    lcd = Lcd(128, 160)
    key_letters = "UDLRS"
    capabilities = frozenset({"storage"})
    return_line = RETURN
    dev_dir = "/devdir"

    def __init__(self, ws: Workspace, log: Callable[[RunRequest], str]) -> None:
        super().__init__(ws)
        self.log = log
        self.requests: list[RunRequest] = []

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def launch(self, req: RunRequest) -> None:
        self.requests.append(req)

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        return True

    def read_log(self, elf: Path | None = None) -> str:
        return self.log(self.requests[-1])


def run(name: str, log: Callable[[RunRequest], str]) -> tuple[Outcome, LogPlayer]:
    with tempfile.TemporaryDirectory() as tmp:
        p = LogPlayer(Workspace(Path(tmp)), log)
        suite = SoakSuite(p, Path("rockbox.elf"), shots=False)
        ctx = engine.StepContext(1, name, p, suite, random.Random(1), Path(tmp), datetime.now())
        return engine.scenarios_for(suite)[name].fn(ctx), p


def test_registered() -> None:
    """Every player gets them (fsck with mass storage); the TS17's own boot,
    tone and poweroff replace the generic ones"""
    ws = Workspace(Path("/nonexistent"))
    table = engine.scenarios_for(SoakSuite(LogPlayer(ws, str), Path("x.elf"), shots=False))
    assert set(table) == GENERIC
    with mock.patch.object(LogPlayer, "capabilities", frozenset()):
        table = engine.scenarios_for(SoakSuite(LogPlayer(ws, str), Path("x.elf"), shots=False))
    assert set(table) == GENERIC - {"fsck"}
    ts17 = engine.scenarios_for(SoakSuite(Ts17(ws), Path("x.elf"), shots=False))
    assert {n for n, s in ts17.items() if s.targets is None} == {"crash", "sdtest", "fsck"}
    assert all(ts17[n].targets == {"ts17"} for n in ("boot", "tone", "poweroff"))


def test_boot() -> None:
    out, _ = run("boot", lambda req: f"booted\n{RETURN} at tick 3000\n")
    assert out.problems == []
    out, _ = run("boot", lambda req: "booted\n")
    assert out.problems == ["early return (hang, reset or power-off)"]


def test_crash() -> None:
    """The panic it asks for is no failure; a missing one is, and so is any
    other fault"""
    panic = "crash test: panic\n*PANIC* dev crash test 42\n"
    out, p = run("crash", lambda req: panic)
    assert (out.problems, p.requests[0].settings["crash"]) == ([], "panic")
    out, _ = run("crash", lambda req: "crash test: panic\n")
    assert out.problems == ["missing: dev crash test 42"]
    out, _ = run("crash", lambda req: panic + "Data abort at 0x1234\n")
    assert out.problems == ["Data abort at 0x1234"]


def test_poweroff() -> None:
    lines = "poweroff test: posting SYS_POWEROFF\npower_off() (dry run): back to the host\n"
    out, p = run("poweroff", lambda req: lines)
    assert (out.problems, p.requests[0].settings["poweroff"]) == ([], "1")
    out, _ = run("poweroff", lambda req: lines.splitlines()[0])
    assert out.problems == ["missing: power_off() (dry run): back to the host"]


def test_sdtest() -> None:
    def log(errors: int) -> Callable[[RunRequest], str]:
        return lambda req: (f"sdtest {req.settings['sdtest'].split()[0]} KiB (4 KiB pieces) "
                            f"at 192 MHz: write 900 ms, read 400 ms, errors {errors}\n{RETURN}\n")
    out, p = run("sdtest", log(0))
    kb, buf, *odd = p.requests[0].settings["sdtest"].split()
    assert out.problems == [] and out.note == " ".join([f"{kb} KiB", buf, *odd])
    out, _ = run("sdtest", log(3))
    assert len(out.problems) == 1 and out.problems[0].endswith("errors 3")
    out, _ = run("sdtest", lambda req: RETURN)
    assert out.problems == ["no sdtest result in the log"]


def test_fsck() -> None:
    @mock.patch.object(LogPlayer, "mass_storage", create=True)
    def check(clean: bool, ms: mock.MagicMock) -> Outcome:
        ms.return_value.__enter__.return_value = Path("/dev/sdx1")
        with mock.patch.object(card, "check", return_value=clean):
            return run("fsck", str)[0]
    assert check(True).problems == []
    assert check(False).problems == ["fsck: ERRORS (udisks' check failed)"]


def test_tone_needs_the_capture() -> None:
    out, p = run("tone", str)
    assert out.skipped and "no Line In capture" in out.note and not p.requests
    with mock.patch.object(soak.linein, "running", return_value=True), \
            mock.patch.object(soak.linein, "tone_check") as tc:
        tc.return_value.problems, tc.return_value.note = [], "10 tone s"
        out, p = run("tone", lambda req: RETURN)
    assert (out.problems, out.note) == ([], "10 tone s")
    assert p.requests[0].settings == {"bluetooth": "off", "volume": "-20",
                                      "play": "/devdir/tone.mp3"}
