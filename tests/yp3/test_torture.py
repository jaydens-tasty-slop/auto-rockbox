"""The torture suite offline: scenario registration, the YP3 shell's replies
and health lines (real ones, from the players' logs and yp3dev's torture runs),
the ledger, and runs through the engine against players whose dev shell lives
in memory: a generic one that crashes, hangs and powers off on cue, and the
YP3 suite on a fake YP3. No device is touched; sleeps take no time."""

from __future__ import annotations

import random
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Self
from unittest import mock

from auto_rockbox import engine
from auto_rockbox.engine import StepContext
from auto_rockbox.lock import DeviceLock
from auto_rockbox.shell import NoShell, Reply, Shell
from auto_rockbox.suites import torture as gen
from auto_rockbox.target import Lcd, RunRequest, Target
from auto_rockbox.targets.yp3 import Yp3
from auto_rockbox.targets.yp3 import torture as yt
from auto_rockbox.workspace import Workspace
from tests import Skip

PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
LEDGER = PORT / "docs/torture-runs.csv"

YP3_WEIGHTS = {"codecs": 12, "skips": 6, "monkey": 8, "plugins": 6, "fs": 4, "db": 4, "usb": 2,
               "longplay": 4, "record": 8, "recui": 4, "fm": 6, "sleep": 4, "sdodd": 4,
               "meta": 4, "theme": 4, "menus": 6, "fmui": 4, "dbdeep": 4, "crawl": 16,
               "codecmix": 8, "shuffle": 8, "folders": 4, "decode": 10, "linein": 4,
               "video": 4}
BT_WEIGHTS = {"bt-onoff": 3, "bt-pcdisc": 2, "bt-pcpower": 0, "bt-avrcp": 2, "bt-churn": 2,
              "bt-disturb": 2, "bt-reboot": 1}

# ---------------------------------------------------------------- samples

# `health` on a build with the activity fields, music paused; an older build
HEALTH = ("health up 1001 s audio 3 elapsed 36076/179252 underruns 0/0 stalls 0 maxstall 0 "
          "ui_late 0 ui_age 6 free 18912 cpu 64 act 1 plugin - db 0")
HEALTH_OLD = ("health up 10 s audio 0 elapsed 0/0 underruns 0/0 stalls 0 maxstall 0 ui_late 0 "
              "ui_age 7 free 114428 cpu 64")
STATUS_STOPPED = "audio 0 elapsed 0/0 ms 0 Hz buffered 0/0 periods 12510 file -"
STATUS_PLAYING = ("audio 1 elapsed 2469/249444 ms 44100 Hz buffered 40968/40968 periods 0 "
                  "file /Eminem/Recovery [Clean Version]/I'm Not Afraid.mp3")
STATUS_STARTING = "audio 6 elapsed 0/0 ms 0 Hz buffered 0/40968 periods 1149 file -"
MSGS = ["msg 19 decode ea613a3d 161280 /Joel Nielsen - BMS - On a Rail 1.mp3",
        "msg 20 splash Bookmark Empty", "msgs next 21"]
BATTERY = "battery 4293 mV 100% charging=0 power=1"
BT_STATUS = ["bt stack: status 3 sink 1, irq stack used 968 of 2048",
             'bt connected 0: AA:BB:CC:DD:EE:10 cod 000000 "test-pc"']
FMMON_0 = ["fmmon: periods=9509 consumed=9508 over=0 under=55 peak 2/174 avg 0/44",
           "mixer chan=1 pcm playing=1 dac periods=77529",
           "mixer gaps: longest 5 ms, 0 over two periods, last call 0 ms ago"]
FMMON_1 = ["fmmon: periods=9736 consumed=9430 over=305 under=1287 peak 552/560 avg 177/179",
           "mixer chan=1 pcm playing=1 dac periods=81320",
           "mixer gaps: longest 19 ms, 74 over two periods, last call 2 ms ago"]
MENUTREE = ["mt 8.2 F Running Time", "mt 8.3 F Legal Notices", "mt 8.4 F Debug (Keep Out!)",
            "mt 9 V Shortcuts", "menutree: 4 nodes"]
LS_ROOT = ["ls /:", "  Bowling for Soup/ 0", "  Whistle Remix.mp3 11221382",
           "  codectest/ 0", "  yp3dev/ 0"]
# The Video Player's dev stats (apps/plugins/videoplayer/stats.c)
VIDEO_END = ["pics end=20 shown=499 drop=0 late=0/6 dec=19.7/20 lcd=0.0 idle=50",
             "sound end=20 under=0/0 low=139/185 cpu=0 read=10/0 lat=0+0"]


# ---------------------------------------------------------------- the clock

class Clock:
    """time.sleep and time.monotonic for runs that take no time"""

    def __init__(self) -> None:
        self.now = 1000.0

    def sleep(self, secs: float) -> None:
        self.now += secs

    def monotonic(self) -> float:
        return self.now


@contextmanager
def fast(clock: Clock) -> Generator[None]:
    with mock.patch("time.sleep", clock.sleep), mock.patch("time.monotonic", clock.monotonic):
        yield


def engine_steps(n: int) -> type[datetime]:
    """The engine's clock, a minute later at each look: a run until
    deadline(n) takes N steps"""
    class Ticks(datetime):
        looks = 0

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            cls.looks += 1
            return cls(2026, 11, 5) + timedelta(minutes=cls.looks)
    return Ticks


def deadline(n: int) -> datetime:
    return datetime(2026, 11, 5) + timedelta(minutes=n + 2)


# ---------------------------------------------------------------- a generic player

class SimPlayer(Target):
    """A player whose image runs in memory: launched, it answers its shell
    until a command sends it back to the host (`!rom`, `crash`, `poweroff`)"""

    name = "sim"
    summary = "the torture tests' player"
    lcd = Lcd(128, 160)
    key_letters = "UDLRS"
    capabilities = frozenset({"shell"})

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self.up = False
        self.launches: list[RunRequest] = []
        self.log = ""
        self.underruns = 0
        self.free = 100000
        self.sim_shell = SimShell(self)

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def launch(self, req: RunRequest) -> None:
        self.launches.append(req)
        self.up, self.log, self.underruns = True, "", 0

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        tick()
        return not self.up

    def read_log(self, elf: Path | None = None) -> str:
        return self.log

    def shell(self) -> Shell:
        return self.sim_shell


class SimShell(Shell):
    def __init__(self, p: SimPlayer) -> None:
        super().__init__("sim", "0000", "0000")
        self.p = p

    def port(self) -> str:
        if not self.p.up:
            raise NoShell("sim: no shell")
        return "/dev/null"

    def __call__(self, line: str, timeout: float = 120.0,
                 echo: Callable[[str], None] | None = None) -> Reply:
        self.port()
        p = self.p
        if line == "crash":
            p.up, p.log = False, "sim boot\n*PANIC* Stkov main\n"
            return Reply(False, ["half a li"], timed_out=True)
        if line == "poweroff":
            p.up, p.log = False, "sim boot\npower_off() (dry run: parking)\n"
            return Reply(False, [], timed_out=True)
        if line == "hang":
            return Reply(False, [], timed_out=True)
        if line == "!rom":
            p.up = False
        elif line == "glitch":
            p.underruns += 1
        elif line == "leak":
            p.free -= 3000
        elif line == "health reset":
            p.underruns = 0
        if line.startswith("health"):
            return Reply(True, [f"health up 5 s audio 0 underruns {p.underruns}/0 stalls 0 "
                                f"ui_late 0 free {p.free} act 1", "ok"])
        return Reply(True, ["ok"])


@gen.player_suite("sim")
class SimSuite(gen.TortureSuite):
    def settled(self, h: gen.Health) -> bool:
        return h.num("act") == 1

    def expected_return(self, records: gen.Records) -> str | None:
        return "power-off (expected)" if "dry run" in dict(records)["dev log (tail)"] else None


def sim_step(command: str, problem: str | None = None) -> Callable[[SimSuite], str]:
    def step(t: SimSuite) -> str:
        h = t.health()
        t.sh(command)
        if problem:
            t.fail(problem)
        t.check_health(h)
        return command
    return step


for _name, _command in (("sim-ok", "work"), ("sim-glitch", "glitch"), ("sim-leak", "leak"),
                        ("sim-crash", "crash"), ("sim-hang", "hang"),
                        ("sim-poweroff", "poweroff")):
    gen.scenario(_name, target={"sim"})(sim_step(_command))
gen.scenario("sim-fail", target={"sim"})(sim_step("work", "a problem of its own"))


def sim_run(root: Path, only: list[str], steps: int, seed: int = 3
            ) -> tuple[SimPlayer, SimSuite, list[engine.Step]]:
    p = SimPlayer(Workspace(root))
    s = SimSuite(p, root / "rockbox.elf", ledger=root / "ledger.csv", seed=seed)
    clock = Clock()
    with fast(clock), mock.patch.object(engine, "datetime", engine_steps(steps)):
        try:
            done = engine.run(s, engine.RunSettings(root / "run", deadline(steps), seed, only),
                              echo=lambda line: None)
        finally:
            s.close()
    return p, s, done


# ---------------------------------------------------------------- a fake YP3

class FakeYp3(Yp3):
    """The YP3 with no transport: a launch boots the shell in memory, ROM
    USB is wherever the shell is not"""

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self.up = False
        self.requests: list[RunRequest] = []
        self.dev = Yp3Shell(self)

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def before_run(self, req: RunRequest) -> None:
        return None

    def launch(self, req: RunRequest) -> None:
        self.requests.append(req)
        self.up = True
        self.dev.boot()

    def shell(self, elf: Path | None = None) -> Shell:
        return self.dev


class Yp3Shell(Shell):
    """The YP3's dev shell (apps/yp3_dev.c) in memory: the commands the
    scenarios under test send, answered as the player does"""

    def __init__(self, p: FakeYp3) -> None:
        super().__init__("yp3", "1209", "0001")
        self.p = p
        self.clock = Clock()
        self.sent: list[str] = []
        self.msgs: list[str] = []           # "KIND TEXT", numbered from 1
        self.charging, self.level = False, 70
        self.glitch_on = ""                 # a command that underruns
        self.boot()

    def boot(self) -> None:
        self.audio, self.path, self.started = 0, "-", 0.0
        self.underruns, self.log, self.msgs = 0, ["Rockbox on YP3"], []

    def port(self) -> str:
        if not self.p.up:
            raise NoShell("yp3: no shell")
        return "/dev/ttyACM0"

    def __call__(self, line: str, timeout: float = 120.0,
                 echo: Callable[[str], None] | None = None) -> Reply:
        self.port()
        assert len(line) <= yt.SHELL_LINE, line
        self.sent.append(line)
        self.log.append(f"sh: {line}")
        if line.split(" ", 1)[0] == self.glitch_on:
            self.underruns += 1
        return Reply(True, [*self.answer(line), "ok"])

    def elapsed(self) -> int:
        return int((self.clock.now - self.started) * 1000) if self.audio == 1 else 0

    def answer(self, line: str) -> list[str]:
        cmd, _, arg = line.partition(" ")
        if cmd == "health":
            if arg == "reset":
                self.underruns = 0
            return [f"health up 30 s audio {self.audio} elapsed {self.elapsed()}/233046 "
                    f"underruns {self.underruns}/0 stalls 0 maxstall 0 ui_late 0 ui_age 6 "
                    "free 18912 cpu 64 act 1 plugin - db 0"]
        if cmd == "play":
            self.audio, self.path, self.started = 1, arg, self.clock.now
        elif cmd == "stop":
            self.audio, self.path = 0, "-"
        elif cmd == "status":
            return [f"audio {self.audio} elapsed {self.elapsed()}/233046 ms 44100 Hz buffered "
                    f"40968/40968 periods 0 file {self.path}" if self.audio else STATUS_STOPPED]
        elif cmd == "log":
            return self.log[-40:]
        elif cmd == "msgs":
            return ([f"msg {i} {m}" for i, m in enumerate(self.msgs, 1) if i > int(arg)]
                    + [f"msgs next {len(self.msgs) + 1}"])
        elif cmd == "bat":
            return [f"battery 4121 mV {self.level}% charging={int(self.charging)} power=1"]
        elif line == "ls /codectest":
            return ["ls /codectest:", "  flac_16_44.flac 1234", "  mp3_320.mp3 5678",
                    "  wv_24_44.wv 999", "  readme.txt 10"]
        elif line == "ls /":
            return ["ls /:", "  Whistle Remix.mp3 11221382", "  codectest/ 0", "  yp3dev/ 0"]
        elif cmd == "meta":
            return ["meta: 1 in 62 ms: length 233046 ms, first frame 4728, bitrate 320, "
                    "sd reads 1"]
        elif cmd == "mkcheck":
            return [f"mkcheck: {arg.split()[1]} files ok"]
        elif cmd == "count":
            return [f"count {arg}: 200"]
        return []


@contextmanager
def yp3_run(root: Path) -> Generator[tuple[FakeYp3, yt.Yp3Torture]]:
    """The YP3 suite started on a fake YP3 (booted, set up, preflight done)"""
    p = FakeYp3(Workspace(root))
    s = yt.Yp3Torture(p, root / "rockbox.elf", seed=1)
    with fast(p.dev.clock), mock.patch.object(yt, "wait_rom",
                                              lambda *a: None if p.up else object()):
        s.start(root)
        try:
            yield p, s
        finally:
            s.close()


def yp3_step(s: yt.Yp3Torture, name: str, seed: int = 1) -> list[str]:
    """One step as the engine runs it: the problems found"""
    ctx = StepContext(1, name, s.target, s, random.Random(seed), s.out, datetime.now())
    outcome = engine.scenarios_for(s)[name].fn(ctx)
    assert not outcome.lost, outcome
    return outcome.problems + s.check(ctx, outcome)


# ---------------------------------------------------------------- tests

def test_registered_for_yp3() -> None:
    ws = Workspace(Path("/nonexistent"))
    table = engine.scenarios_for(yt.Yp3Torture(Yp3(ws), Path("x.elf")))
    assert {n: s.weight for n, s in table.items()} == YP3_WEIGHTS
    assert all(s.targets == {"yp3"} for s in table.values())
    # this PC as the headset only with the player's address configured
    bt = Workspace(Path("/nonexistent"), {"bluetooth": {"player": "aa:bb:cc:dd:ee:ff"}})
    suite = yt.Yp3Torture(Yp3(bt), Path("x.elf"))
    table = engine.scenarios_for(suite)
    assert {n: s.weight for n, s in table.items()} == YP3_WEIGHTS | BT_WEIGHTS
    assert all(table[n].needs == {yt.BT_PC} for n in BT_WEIGHTS)
    assert suite.pc and suite.pc.player == "AA:BB:CC:DD:EE:FF" and suite.pc.sink == "arb_null"
    assert gen.suite_class("yp3") is yt.Yp3Torture


def test_health_lines() -> None:
    h = gen.parse_health(["ok?", HEALTH], ("plugin",))
    assert h is not None
    assert (h.num("audio"), h.pair("elapsed"), h.num("underruns"), h.num("free")) == \
        (3, (36076, 179252), 0, 18912)
    assert (h.num("act"), h.text("plugin"), h.num("db")) == (1, "-", 0)
    old = gen.parse_health([HEALTH_OLD], ("plugin",))
    assert old is not None and (old.num("act", -1), old.text("plugin"), old.num("ui_age")) == \
        (-1, "-", 7)
    assert gen.parse_health(["health: busy", STATUS_PLAYING]) is None


def test_status_and_messages() -> None:
    assert yt.parse_status([STATUS_STOPPED]) == yt.Status(0, 0, 0, 0, "-")
    s = yt.parse_status(["bt: media 689 pkts bp 53 behind 0 ms waits 0 restarts 0 bufs 6/9",
                         STATUS_PLAYING])
    assert (s.audio, s.elapsed, s.length, s.hz) == (1, 2469, 249444, 44100)
    assert s.path == "/Eminem/Recovery [Clean Version]/I'm Not Afraid.mp3"
    assert yt.parse_status([STATUS_STARTING]).audio == 6
    assert yt.parse_status([]).audio == -1
    nxt, msgs = yt.parse_msgs(["msgs lost 3", *MSGS])
    assert nxt == 21 and msgs == [
        ("lost", "3"), ("decode", "ea613a3d 161280 /Joel Nielsen - BMS - On a Rail 1.mp3"),
        ("splash", "Bookmark Empty")]
    assert yt.parse_msgs(["err unknown command"]) == (None, [])


def test_message_judging() -> None:
    assert yt.message_problems("menus", [("splash", "Bookmark Empty")]) == []
    prompt = ("yesno", "Not enough memory for / the database")
    assert yt.message_problems("db", [prompt]) == []
    assert yt.message_problems("menus", [prompt]) == \
        ['yesno "Not enough memory for / the database"']
    # a plugin known to fail without its data, and the splash that says why
    dict_rock = [("splash", "Failed to open index"),
                 ("plugin", "/.rockbox/rocks/apps/dict.rock returned error")]
    assert yt.message_problems("plugins", dict_rock) == []
    other = [("plugin", "/.rockbox/rocks/games/rockblox.rock returned error")]
    assert yt.message_problems("plugins", other) == [f'plugin "{other[0][1]}"']
    leak = [("plugin", "/.rockbox/rocks/apps/foo.rock leaks file handles")]
    assert len(yt.message_problems("plugins", leak)) == 1
    # hashes, video stats and lost counts are judged elsewhere
    assert yt.message_problems("decode", [("decode", "deadbeef 1 /failed.mp3"),
                                          ("lost", "2")]) == []


def test_decode_hashes_and_video() -> None:
    d = yt.DecodeHashes()
    assert d.add("ea613a3d 161280 /a b.mp3") is None
    assert d.add("ea613a3d 161280 /a b.mp3") is None
    assert d.add("ea613a3d 200000 /a b.mp3") is None          # another length: another key
    assert d.add("00000000 161280 /a b.mp3") == \
        "decode hash 00000000 differs from ea613a3d (/a b.mp3, 161280 frames)"
    assert d.line() == ("Decode hashes: 4 plays of 2 (file, frames), 1 played more than once, "
                        "1 differences")
    v = yt.VideoStats()
    assert v.add("pics t=10 shown=248 drop=0 late=0/6 dec=19.9/20 lcd=0.0 idle=50") is None
    assert [v.add(x) for x in VIDEO_END] == [None, None]
    assert v.add("pics end=20 shown=400 drop=99 late=3/40 dec=19.7/20 lcd=0.0 idle=10") == \
        "video dropped 99 of 499 pictures"
    assert v.add("sound end=20 under=2/80 low=139/185 cpu=0 read=31/0 lat=0+0") == \
        "video underruns 2/80 (count/ms)"
    assert v.add("cpu1 stopped (fault pc 0080abcd)") == "video: cpu1 stopped (fault pc 0080abcd)"
    assert v.line() == ("Video: 2 plays, 99 of 998 pictures dropped, 3 late (worst 40 ms), idle "
                        "10..50%; sound underruns 2, slowest read 31 ms")


def test_other_replies() -> None:
    assert yt.parse_battery(["bt: x", BATTERY]) == (4293, 100, False)
    assert yt.parse_battery(["battery 4121 mV 69% charging=1 power=1"]) == (4121, 69, True)
    assert yt.ls_entries(LS_ROOT) == ["Bowling for Soup/", "Whistle Remix.mp3", "codectest/",
                                      "yp3dev/"]
    assert yt.ls_entries(["  sd: read error 5 0"]) == []          # a log line in between
    assert [(n.path, n.kind, n.name) for n in yt.parse_menutree(MENUTREE)] == [
        ("8.2", "F", "Running Time"), ("8.3", "F", "Legal Notices"),
        ("8.4", "F", "Debug (Keep Out!)"), ("9", "V", "Shortcuts")]
    assert yt.fm_periods(" ".join(FMMON_0)) == (9509, 0)
    assert yt.fm_periods(" ".join(FMMON_1)) == (9736, 305)
    assert yt.fm_consumer(" ".join(FMMON_0), " ".join(FMMON_1)) == (
        "; mixer channel playing, pcm playing 1, DAC periods +3791; longest mixer gap 19 ms, "
        "74 over two periods, last call 2 ms ago")
    assert yt.parse_bt_status(BT_STATUS) == yt.BtState(3, 1, 968, 2048, "AA:BB:CC:DD:EE:10")
    off = yt.parse_bt_status(["bt stack: status 1 sink 0, irq stack used 1208 of 2048"])
    assert off == yt.BtState(1, 0, 1208, 2048, None)
    assert yt.parse_bt_status(["err unknown command"]) is None
    assert yt.r32_words(["40080068: 10000003", "bt: 12345678: 1"]) == [0x10000003]


def test_link_table() -> None:
    raw = bytearray(52)
    raw[7:14] = bytes([1, 0x10, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])     # entry 1, BD_ADDR LSB first
    raw[14:21] = bytes([0, 1, 2, 3, 4, 5, 6])                     # entry 2: not in use
    lines = [f"{yt.BT_LINK_TBL + 4 * i:08x}: {int.from_bytes(raw[4 * i:4 * i + 4], 'little'):08x}"
             for i in range(13)]
    assert yt.link_table(yt.r32_words(lines)) == ["AA:BB:CC:DD:EE:10"]


def test_no_folder_skip() -> None:
    assert yt.no_folder_skip(["R", "R+", "L", "300", "L+", "S", "R+"]) == \
        ["R", "1500", "R+", "L", "300", "1500", "L+", "S", "R+"]


def test_free_drift() -> None:
    d = gen.FreeDrift(budget=16 * 1024, window=8)
    assert [d.add(100000) for _ in range(8)] == [None] * 8
    assert d.add(60000) is None                  # one dip: the window's ceiling holds
    assert [d.add(90000) for _ in range(7)] == [None] * 7      # within the budget
    lows = [d.add(80000) for _ in range(8)]
    assert lows[:7] == [None] * 7
    assert lows[7] == ("free memory ceiling 80000 B, 20000 B down over 24 settled readings "
                       "since the boot")
    assert [d.add(80000) for _ in range(8)] == [None] * 8      # measured from there on
    d.reset()
    assert d.reference is None and not d.readings


def test_ledger() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "docs" / "ledger.csv"
        row = gen.LedgerRow("20261105-101500", 1.234, 40, 2, 1, 7, "codecs fs")
        gen.ledger_append(path, row)
        gen.ledger_append(path, row)
        assert path.read_text().splitlines()[0] == gen.LEDGER_HEAD
        assert gen.ledger_rows(path) == [gen.LedgerRow("20261105-101500", 1.23, 40, 2, 1, 7,
                                                       "codecs fs")] * 2
    assert gen.ledger_totals([]) == ["no torture runs in the ledger"]


def test_real_ledger() -> None:
    """yp3dev's ledger reads as the suite writes it (a note after the
    scenarios has commas of its own)"""
    if not LEDGER.exists():
        raise Skip(f"no {LEDGER}")
    lines = LEDGER.read_text().splitlines()
    assert lines[0] == gen.LEDGER_HEAD
    rows = gen.ledger_rows(LEDGER)
    assert len(rows) == len([x for x in lines[1:] if x])
    assert [r.csv().split(",", 1)[0] for r in rows] == [x.split(",", 1)[0] for x in lines[1:] if x]
    totals = gen.ledger_totals(rows)
    assert totals[0].startswith(f"{len(rows)} torture runs on hardware")


def test_keys_fit_the_shell_line() -> None:
    with tempfile.TemporaryDirectory() as tmp, yp3_run(Path(tmp)) as (p, s):
        script = " ".join(random.Random(4).choices(yt.MONKEY_KEYS, yt.MONKEY_WEIGHTS, k=60))
        p.dev.sent.clear()
        s.keys(script)
        assert len(p.dev.sent) > 1 and all(x.startswith("keys ") for x in p.dev.sent)
        assert " ".join(x.removeprefix("keys ") for x in p.dev.sent) == script


def test_yp3_boot_and_steps() -> None:
    with tempfile.TemporaryDirectory() as tmp, yp3_run(Path(tmp)) as (p, s):
        # the boot: the settings files on the card, the charge limit, the module probe
        req = p.requests[0]
        assert isinstance(req, yt.Yp3Run) and req.seconds == 0 and req.usb
        assert req.settings == {"speaker": "0"}
        assert [sd for _, sd in req.pushes] == [f"/yp3dev/{n}" for n in yt.CFGS]
        assert (Path(tmp) / "t_limit.cfg").read_text() == "charge limit: 80\n"
        assert {"dryoff", "cfg /yp3dev/t_limit.cfg", f"play {yt.PROBE}"} <= set(p.dev.sent)
        for name in ("codecs", "decode", "meta", "fs"):
            assert yp3_step(s, name) == [], name
        assert any(x.startswith("mkcheck /torture") for x in p.dev.sent)
        # what every step is checked for
        p.dev.msgs.append("splash Error accessing playlist control file")
        assert yp3_step(s, "meta") == \
            ['messages: splash "Error accessing playlist control file"']
        p.dev.msgs.append("decode ea613a3d 161280 /codectest/mp3_320.mp3")
        assert yp3_step(s, "meta") == []
        p.dev.msgs.append("decode 0badf00d 161280 /codectest/mp3_320.mp3")
        assert yp3_step(s, "meta") == ["messages: decode hash 0badf00d differs from ea613a3d "
                                       "(/codectest/mp3_320.mp3, 161280 frames)"]
        p.dev.charging, p.dev.level = True, 85
        assert yp3_step(s, "meta") == ["charging at 85% (4121 mV), limit 80%"]
        p.dev.charging = False
        p.dev.glitch_on = "mkfiles"
        assert yp3_step(s, "fs") == ["underruns +1"]
        assert s.tally == {"codecs": [1, 0], "decode": [1, 0], "meta": [5, 3], "fs": [2, 1]}


def test_yp3_expected_return() -> None:
    s = yt.Yp3Torture(Yp3(Workspace(Path("/nonexistent"))), Path("x.elf"))
    assert s.expected_return([("dev log (tail)", "x\npower_off() (dry run: parking)\n")]) == \
        "SHUTDOWN (menu power-off, expected)"
    assert s.expected_return([("dev log (tail)", "HANG: main 12 s\n")]) is None
    assert s.crash_re.search("HANG: main thread stuck") and s.crash_re.search("*PANIC* stkov")


def test_simulated_run() -> None:
    """Steps through the engine: problems, a crash, a hang, a power-off the
    player expects, each collected and the player booted again; the report
    and the ledger line"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        only = ["sim-ok", "sim-fail", "sim-glitch", "sim-crash", "sim-hang", "sim-poweroff"]
        p, s, steps = sim_run(root, only, 24)
        assert len(steps) == 24
        by = {n: [x for x in steps if x.name == n] for n in only}
        assert all(by.values()), {n: len(v) for n, v in by.items()}
        assert all(x.ok for x in by["sim-ok"] + by["sim-poweroff"])
        assert all(x.problems == ["a problem of its own"] for x in by["sim-fail"])
        assert all(x.problems == ["underruns +1"] for x in by["sim-glitch"])
        crashed = by["sim-crash"] + by["sim-hang"]
        assert all(x.problems[0] == "LOST" for x in crashed)
        out = root / "run"
        crash_files = sorted(out.glob("crash-*.txt"))
        assert len(crash_files) == len(crashed) + len(by["sim-poweroff"])
        texts = [f.read_text() for f in crash_files]
        assert sum("== crash: *PANIC* Stkov main" in t for t in texts) == len(by["sim-crash"])
        assert sum(t.startswith("no reply to: hang") for t in texts) == len(by["sim-hang"])
        assert sum(t.startswith("power-off (expected): shell gone during: poweroff")
                   for t in texts) == len(by["sim-poweroff"])
        assert all("== commands" in t and "== dev log (tail)" in t for t in texts)
        assert "'half a li'" in next(t for t in texts if "PANIC" in t)
        # a boot for the run, one after each return
        assert len(p.launches) == 1 + len(crash_files)
        assert all(r.seconds == 0 and r.usb for r in p.launches)
        assert (s.crashes, s.returns) == (len(crashed), len(by["sim-poweroff"]))
        report = (out / "report.md").read_text()
        assert f"crashes/hangs: {len(crashed)}, expected returns: {len(by['sim-poweroff'])}" \
            in report
        assert f"| sim-glitch | {len(by['sim-glitch'])} | {len(by['sim-glitch'])} |" in report
        (row,) = gen.ledger_rows(root / "ledger.csv")
        assert (row.steps, row.failures, row.crashes, row.seed) == \
            (24, sum(not x.ok for x in steps), len(crashed), 3)
        assert row.scenarios == " ".join(sorted(only))


def test_simulated_leak() -> None:
    """Free memory read after settled steps: a slow leak fails the step where
    the ceiling falls past the budget, once"""
    with tempfile.TemporaryDirectory() as tmp:
        _, _, steps = sim_run(Path(tmp), ["sim-leak"], 16)
    failed = [x for x in steps if not x.ok]
    # 3000 B a step: the first 8 readings make the reference (97000); the
    # ceiling of the last 8 (79000 at reading 14) is the first over 16 KiB below
    assert [x.step for x in failed] == [14]
    assert failed[0].problems[0].startswith("free memory ceiling 79000 B, 18000 B down")
