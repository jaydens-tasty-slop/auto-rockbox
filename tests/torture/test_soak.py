"""Every player's soak scenarios (suites/soak.py) offline, on a player whose
image is back at once with a log made from the run's settings: which
players get them, what each boot asks for, and how its log is judged."""

from __future__ import annotations

import random
import tempfile
import wave
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from unittest import mock

import numpy as np

from auto_rockbox import card, engine, keys, registry
from auto_rockbox.engine import Outcome
from auto_rockbox.lock import DeviceLock
from auto_rockbox.suites import soak
from auto_rockbox.suites.soak import SoakSuite
from auto_rockbox.target import Lcd, RunRequest, Target
from auto_rockbox.targets.ts17 import Ts17
from auto_rockbox.targets.yp3 import Yp3
from auto_rockbox.workspace import Workspace

RETURN = "dev return: on time"
GENERIC = {"boot", "crash", "poweroff", "sdtest", "fsck", "tone", "games", "usb-hash",
           "udelay", "backlight", "keys"}


class LogPlayer(Target):
    """A player whose image is back at once; its log is LOG(request)"""

    name = "logfake"
    summary = "the soak tests' player"
    lcd = Lcd(128, 160)
    key_letters = "UDLRS"
    capabilities = frozenset({"storage", "usb-poweroff"})
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
    """Every player gets them (fsck with mass storage, poweroff with a build
    that powers off on USB); the TS17's own boot, tone and poweroff replace
    the generic ones"""
    ws = Workspace(Path("/nonexistent"))
    table = engine.scenarios_for(SoakSuite(LogPlayer(ws, str), Path("x.elf"), shots=False))
    assert set(table) == GENERIC
    with mock.patch.object(LogPlayer, "capabilities", frozenset()):
        table = engine.scenarios_for(SoakSuite(LogPlayer(ws, str), Path("x.elf"), shots=False))
    assert set(table) == GENERIC - {"fsck", "poweroff", "usb-hash"}
    yp3 = SoakSuite(Yp3(ws), Path("x.elf"), shots=False)
    assert engine.left_out(yp3) == {"poweroff": "needs usb-poweroff", "flick": "needs touch",
                                    "fm-station": "needs fm", "fm-record": "needs fm"}
    ts17 = engine.scenarios_for(SoakSuite(Ts17(ws), Path("x.elf"), shots=False))
    assert ({n for n, s in ts17.items() if s.targets is None}
            == {"crash", "sdtest", "fsck", "games", "usb-hash", "udelay", "backlight", "keys"})
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
        with mock.patch.object(card, "check", return_value=card.Check(clean)):
            return run("fsck", str)[0]
    assert check(True).problems == []
    assert check(False).problems == [card.Check(False).describe()]


def test_usb_hash() -> None:
    """The file written in one session reads back in the next and is gone
    after; a changed byte fails"""
    with tempfile.TemporaryDirectory() as card_dir:
        mp = Path(card_dir)
        reads: list[bytes] = []

        @contextmanager
        def mounted(dev: Path) -> Generator[Path]:
            yield mp

        def check(flip: bool) -> Outcome:
            real = Path.read_bytes

            def read(path: Path) -> bytes:
                b = real(path)
                reads.append(b)
                return bytes([b[0] ^ 1]) + b[1:] if flip else b
            with mock.patch.object(LogPlayer, "mass_storage", create=True) as ms, \
                    mock.patch.object(card, "mounted", mounted), \
                    mock.patch.object(Path, "read_bytes", read):
                ms.return_value.__enter__.return_value = Path("/dev/sdx1")
                return run("usb-hash", str)[0]
        out = check(False)
        assert out.problems == [] and out.note.startswith("16 MiB written")
        assert len(reads[0]) == 16 << 20 and not list(mp.iterdir())
        assert check(True).problems[0].startswith("read back sha256")


def test_udelay() -> None:
    """Every length at both clocks, none short or long; a build without the
    command skips"""
    def lines(short: int = 0) -> str:
        return "".join(f"udelay {us} us x {max(1, 10**6 // us)} at {mhz} MHz: 100 ticks, "
                       f"{us * (0.5 if us == short else 1) + (0.5 if us == 1 else 0):.3f} "
                       "us a call\n"
                       for mhz in (96, 128) for us in (10 ** e for e in range(7)))
    out, p = run("udelay", lambda req: lines() + RETURN)
    assert out.problems == [] and out.note == ("udelay(1) 1.500 us at 96 MHz, "
                                               "udelay(1) 1.500 us at 128 MHz")
    assert p.requests[0].settings == {"sh": "udelay;cpu max;udelay"}
    out, _ = run("udelay", lambda req: lines(short=1000) + RETURN)
    assert out.problems == ["udelay(1000) at 96 MHz took 500.000 us",
                            "udelay(1000) at 128 MHz took 500.000 us"]
    assert run("udelay", lambda req: "udelay 1 us x 1000000 at 96 MHz: 150 ticks, 1.500 "
               "us a call\n" + RETURN)[0].problems == ["1 udelay lines of 14"]
    assert run("udelay", lambda req: "sh: udelay: err\n" + RETURN)[0].skipped


def test_backlight_curve() -> None:
    """Each step's panel brightness from the frames inside it; a step with
    no frame is None; a panel that never lit gives nothing"""
    steps = [(0, 15.0), (1, 25.0), (2, 35.0), (3, 45.0)]
    frames = {}
    for t in range(0, 60, 5):
        f = np.full((40, 40), 3.0, np.float32)
        level = max((lv for lv, at in steps if t - 2.5 >= at), default=None)
        if level:
            f[10:30, 10:30] = 60.0 * level      # the panel; the rest is the room
        frames[t] = f
    curve, panel = soak.backlight_curve(frames, steps)
    assert panel == 400 and curve == [(0, 3.0), (1, 60.0), (2, 120.0), (3, 180.0)]
    del frames[40]
    assert soak.backlight_curve(frames, steps)[0][2] == (2, None)
    dark = {t: np.zeros((40, 40), np.float32) for t in frames}
    assert soak.backlight_curve(dark, steps) == ([(0, None), (1, None), (2, None), (3, None)], 0)


def test_backlight_judging() -> None:
    """Off, then every level no darker than the one before, the lowest
    clearly lit; a camera that is not running skips"""
    log = "".join(f"backlight {lv} at tick {1500 + 1000 * lv}\n" for lv in range(4))

    def judge(curve: list[tuple[int, float | None]]) -> Outcome:
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(soak.camera, "check_fresh"), \
                mock.patch.object(soak, "backlight_curve", return_value=(curve, 5000)):
            p = LogPlayer(Workspace(Path(tmp)), lambda req: log + RETURN)
            ctx = engine.StepContext(1, "backlight", p, SoakSuite(p, Path("x.elf"), shots=False),
                                     random.Random(1), Path(tmp), datetime.now())
            out = engine.scenarios_for(ctx.suite)["backlight"].fn(ctx)
            assert p.requests[0].settings == {"then": "backlight"}
            return out
    assert judge([(0, 1.0), (1, 49.0), (2, 185.0), (3, 185.2)]).problems == []
    assert judge([(0, 1.0), (1, 49.0), (2, 40.0), (3, 190.0)]).problems == [
        "brightness 2 darker on camera than 1 (40.0 < 49.0)"]
    assert judge([(0, 1.0), (1, 4.0), (2, 185.0), (3, None)]).problems == [
        "no frame inside brightness 3", "brightness 1 hardly lit (3.0 over off)"]
    with mock.patch.object(soak.camera, "check_fresh", side_effect=soak.camera.CameraError("x")):
        assert run("backlight", str)[0].skipped


def test_keys() -> None:
    """Each letter tapped and the first held, read back from the keylog: a
    tap that reads as another key, a lost press or a short hold fails"""
    def keylog(states: list[int], hold: int = 100) -> Callable[[RunRequest], str]:
        lines, tick = [], 300
        for i, st in enumerate(states):
            lines += [f"buttons {st:x} at tick {tick}",
                      f"buttons 0 at tick {tick + (hold if i == len(states) - 1 else 15)}"]
            tick += 60 if i < len(states) - 1 else hold + 10
        return lambda req: "\n".join(lines) + "\n" + RETURN
    out, p = run("keys", keylog([1, 2, 4, 8, 16, 1]))
    assert out.problems == [] and out.note == "U 1, D 2, L 4, R 8, S 10"
    assert p.requests[0].keys == "3000 U 400 D 400 L 400 R 400 S 600 U+ 1000"
    assert p.requests[0].settings == {"sh": "keylog 20"}
    assert run("keys", keylog([1, 2, 2, 8, 16, 1]))[0].problems == ["L reads as D (2)"]
    assert run("keys", keylog([1, 2, 4, 8, 1]))[0].problems == [
        "5 presses in the keylog of 6"]
    assert run("keys", keylog([1, 2, 4, 8, 16, 1], hold=30))[0].problems == [
        "U held 30 ticks, not a second"]
    assert soak.key_presses("buttons 4 (adc 812)\nbuttons 0 (adc 4000)\n") == [(4, None)]


def test_fm_record() -> None:
    """The recording's log and its WAV on the card: whole, loud enough, no
    wraps, then removed; a quiet or short file fails"""
    def check(seconds: float, level: float, log: str = "rec: 10000 ms recorded\n") -> Outcome:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ws = Workspace(root, {"fm": {"station": 94900}})
            (root / "devdir").mkdir()
            wav = root / "devdir" / "arb-fmrec.wav"
            t = np.arange(int(44100 * seconds)) / 44100
            x = (level * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2")
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(2)
                w.setsampwidth(2)
                w.setframerate(44100)
                w.writeframes(np.repeat(x, 2).tobytes())

            @contextmanager
            def mounted(dev: Path) -> Generator[Path]:
                yield root
            p = LogPlayer(ws, lambda req: log + RETURN)
            with mock.patch.object(LogPlayer, "capabilities", frozenset({"fm", "storage"})), \
                    mock.patch.object(LogPlayer, "mass_storage", create=True) as ms, \
                    mock.patch.object(card, "mounted", mounted):
                ms.return_value.__enter__.return_value = Path("/dev/sdx1")
                suite = SoakSuite(p, Path("x.elf"), shots=False)
                ctx = engine.StepContext(1, "fm-record", p, suite, random.Random(1), root,
                                         datetime.now())
                out = engine.scenarios_for(suite)["fm-record"].fn(ctx)
            assert not wav.exists()
            assert (root / "001-fm-record.wav").exists() == bool(out.problems)
            assert p.requests[0].settings["then"] == "rec 10 arb-fmrec"
            return out
    out = check(10.0, 0.3)
    assert out.problems == [] and out.note.startswith("10.0 s, RMS -13.5 dBFS")
    assert check(10.0, 0.001).problems[0].startswith("the recording is quiet")
    assert check(6.0, 0.3).problems == ["the file holds 6.0 s of the 10.0 s recorded"]
    assert check(10.0, 0.3, "rec: warnings 1 at 500 ms\nrec: 10000 ms recorded\n").problems == [
        "rec: warnings 1 at 500 ms"]


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


def test_games() -> None:
    """Each game in turn by step, run at boot with its script: PLUGIN_OK
    passes; a game the keys cannot leave, or one that never started, fails"""
    pong = "/.rockbox/rocks/games/pong.rock"
    games = {"games/pong.rock": "S D", "games/snake.rock": "S S D"}

    def played(log: str) -> tuple[Outcome, LogPlayer]:
        with mock.patch.object(LogPlayer, "games", games):
            return run("games", lambda req: log + RETURN)
    out, p = played(f"run {pong}\nrun {pong}: rc 0\n")
    assert (out.problems, out.note) == ([], "games/pong.rock")
    assert (p.requests[0].settings, p.requests[0].keys) == ({"sh": f"run {pong}"}, "S D")
    assert played(f"run {pong}\n")[0].problems == [
        "did not quit with the player's keys (a keymap bug?)"]
    assert played(f"run {pong}\nrun {pong}: rc -1\n")[0].problems == [
        "returned -1, not PLUGIN_OK"]
    assert played("")[0].problems == ["the game did not start"]
    out, p = run("games", str)
    assert out.skipped and not p.requests


def test_game_scripts_fit() -> None:
    """Every player's game scripts are key scripts its firmware takes"""
    ws = Workspace(Path("/nonexistent"))
    for cls in registry.targets().values():
        t = cls(ws)
        for rock, script in t.games.items():
            assert len(script) <= t.keys_max, rock
            keys.check_script(script, t.key_letters, t.key_suffixes,
                              (t.lcd.width, t.lcd.height) if "touch" in t.capabilities else None)


def test_shots_need_the_webcam() -> None:
    """A run with shots checks the webcam publisher before its first step"""
    with tempfile.TemporaryDirectory() as tmp:
        p = LogPlayer(Workspace(Path(tmp)), str)
        with mock.patch.object(soak.camera, "check_fresh") as fresh:
            SoakSuite(p, Path("x.elf"), shots=False).start(Path(tmp))
            fresh.assert_not_called()
            SoakSuite(p, Path("x.elf"), shots=True).start(Path(tmp))
            fresh.assert_called_once()


def test_the_same_error_ends_a_run() -> None:
    """Steps that raise the same error three times running end the run
    instead of spinning until its deadline; an error between good steps
    does not"""
    calls: list[int] = []

    def broken(step: engine.StepContext) -> Outcome:
        calls.append(step.index)
        raise OSError("no webcam")

    def flaky(step: engine.StepContext) -> Outcome:
        if step.index % 2:
            raise OSError("once")
        return Outcome()
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        p = LogPlayer(Workspace(Path(tmp)), str)
        suite = SoakSuite(p, Path("x.elf"), shots=False)
        for fn, n in ((broken, 3), (flaky, 8)):
            table = {"x": engine.Scenario("x", fn, "soak", "boot")}
            steps = engine._steps(suite, Path(tmp) / fn.__name__, table, iter(["x"] * 8),
                                  rng=random.Random(1), echo=lines.append, head="")
            assert len(steps) == n, fn.__name__
    assert calls == [1, 2, 3]
    assert any(line.startswith("stopped: the same error 3 steps running") for line in lines)
