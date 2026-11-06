"""The torture suite offline: scenario registration, the shared dev shell's
replies and health lines (real ones, from the players' logs and yp3dev's
torture runs), the ledger, and runs through the engine against players whose
dev shell lives in memory (tests/torture/fakes.py): a generic one that
crashes, hangs and powers off on cue, and a Rockbox player with
RockboxTorture's defaults. No device is touched; sleeps take no time."""

from __future__ import annotations

import importlib.util
import random
import re
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from auto_rockbox import engine
from auto_rockbox.btpeer import analysis
from auto_rockbox.btpeer.hidkeys import keys_for
from auto_rockbox.suites import rockbox_torture as rt
from auto_rockbox.suites import torture as gen
from auto_rockbox.workspace import Workspace
from tests import Skip

from .fakes import (
    PC,
    PLAYER,
    STATUS_STOPPED,
    FakePcLink,
    FakePlayer,
    FakeTorture,
    SimPlayer,
    SimSuite,
    rockbox_run,
    sim_run,
    step,
    step_outcome,
    tone_wav,
)

PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
LEDGER = PORT / "docs/torture-runs.csv"

# Every player's scenarios, and those that need a feature of the player's build
SHARED_WEIGHTS = {"codecs": 12, "skips": 6, "plugins": 6, "fs": 4, "db": 4, "usb": 2,
                  "monkey": 8, "menus": 6, "dbdeep": 4, "crawl": 16, "recui": 4, "fmui": 4,
                  "longplay": 4, "record": 8, "sdodd": 4, "meta": 4, "theme": 4, "codecmix": 8,
                  "shuffle": 8, "folders": 4, "decode": 10, "linein": 4, "video": 4, "missing": 2}
FEATURE_WEIGHTS = {"sleep": 4}
BT_WEIGHTS = {"bt-onoff": 3, "bt-pcdisc": 2, "bt-pcpower": 0, "bt-avrcp": 2, "bt-churn": 2,
              "bt-rate": 2, "bt-disturb": 2, "bt-reboot": 1, "bt-forget": 1, "bt-capture": 2}
BUMBLE_WEIGHTS = {"bt-headset": 2}
HID_WEIGHTS = {"bt-hid": 2}             # with Bumble and a build with Bluetooth HID

# ---------------------------------------------------------------- samples

# `health` on a build with the activity fields, music paused; an older build
HEALTH = ("health up 1001 s audio 3 elapsed 36076/179252 underruns 0/0 stalls 0 maxstall 0 "
          "ui_late 0 ui_age 6 free 18912 cpu 64 act 1 plugin - db 0")
HEALTH_OLD = ("health up 10 s audio 0 elapsed 0/0 underruns 0/0 stalls 0 maxstall 0 ui_late 0 "
              "ui_age 7 free 114428 cpu 64")
STATUS_PLAYING = ("audio 1 elapsed 2469/249444 ms 44100 Hz buffered 40968/40968 periods 0 "
                  "file /Eminem/Recovery [Clean Version]/I'm Not Afraid.mp3")
STATUS_STARTING = "audio 6 elapsed 0/0 ms 0 Hz buffered 0/40968 periods 1149 file -"
MSGS = ["msg 19 decode ea613a3d 161280 /Joel Nielsen - BMS - On a Rail 1.mp3",
        "msg 20 splash Bookmark Empty", "msgs next 21"]
BATTERY = "battery 4293 mV 100% charging=0 power=1"
BT_STATUS = ["bt stack: status 3 sink 1, irq stack used 968 of 2048",
             'bt connected 0: AA:BB:CC:DD:EE:10 cod 000000 "test-pc"']
MENUTREE = ["mt 8.2 F Running Time", "mt 8.3 F Legal Notices", "mt 8.4 F Debug (Keep Out!)",
            "mt 9 V Shortcuts", "menutree: 4 nodes"]
LS_ROOT = ["ls /:", "  Bowling for Soup/ 0", "  Whistle Remix.mp3 11221382",
           "  codectest/ 0", "  yp3dev/ 0"]
# The Video Player's dev stats (apps/plugins/videoplayer/stats.c)
VIDEO_END = ["pics end=20 shown=499 drop=0 late=0/6 dec=19.7/20 lcd=0.0 idle=50",
             "sound end=20 under=0/0 low=139/185 cpu=0 read=10/0 lat=0+0"]


# ---------------------------------------------------------------- tests

def test_registered_for_every_rockbox_player() -> None:
    """The shared scenarios for any player's RockboxTorture, with no target;
    those that need a feature only with it, the bt-* ones only with the
    player's address; none for a suite that is not a RockboxTorture"""
    ws = Workspace(Path("/nonexistent"))
    table = engine.scenarios_for(FakeTorture(FakePlayer(ws), Path("x.elf")))
    assert {n: s.weight for n, s in table.items()} == SHARED_WEIGHTS
    assert all(s.targets is None and rt.ROCKBOX in s.needs for s in table.values())
    with mock.patch.object(FakeTorture, "features", frozenset({rt.LCD_GATE})):
        table = engine.scenarios_for(FakeTorture(FakePlayer(ws), Path("x.elf")))
    assert {n: s.weight for n, s in table.items()} == SHARED_WEIGHTS | FEATURE_WEIGHTS
    assert (table["menus"].needs, table["sleep"].needs) == (
        {rt.ROCKBOX}, {rt.ROCKBOX, rt.LCD_GATE})
    # this PC as the headset only with the player's address configured
    bt = Workspace(Path("/nonexistent"),
                   {"targets": {"rbfake": {"bt_address": "aa:bb:cc:dd:ee:ff"}}})
    suite = FakeTorture(FakePlayer(bt), Path("x.elf"))
    table = engine.scenarios_for(suite)
    assert {n: s.weight for n, s in table.items()} == SHARED_WEIGHTS | BT_WEIGHTS
    assert all(table[n].needs == {rt.ROCKBOX, rt.BT_PC} for n in BT_WEIGHTS)
    assert suite.pc and suite.pc.player == "AA:BB:CC:DD:EE:FF" and suite.pc.sink == "arb_null"
    # and the adapter lent to a Bumble peer, with Bumble and the adapter's transport
    bt = Workspace(Path("/nonexistent"), {"targets": {"rbfake": {"bt_address": PLAYER}},
                                          "bluetooth": {"adapter": "usb:10d7:b012"}})
    for have, extra in ((False, {}), (True, BUMBLE_WEIGHTS)):
        with mock.patch.object(rt, "have_bumble", return_value=have):
            suite = FakeTorture(FakePlayer(bt), Path("x.elf"))
        table = engine.scenarios_for(suite)
        assert {n: s.weight for n, s in table.items()} == SHARED_WEIGHTS | BT_WEIGHTS | extra
    assert all(table[n].needs == {rt.ROCKBOX, rt.BT_PC, rt.BT_BUMBLE} for n in BUMBLE_WEIGHTS)
    with mock.patch.object(rt, "have_bumble", return_value=True), \
            mock.patch.object(FakeTorture, "features", frozenset({rt.BT_HID})):
        table = engine.scenarios_for(FakeTorture(FakePlayer(bt), Path("x.elf")))
    assert {n: s.weight for n, s in table.items()} == \
        SHARED_WEIGHTS | BT_WEIGHTS | BUMBLE_WEIGHTS | HID_WEIGHTS
    assert table["bt-hid"].needs == {rt.ROCKBOX, rt.BT_PC, rt.BT_BUMBLE, rt.BT_HID}
    # an address in [bluetooth] is the host commands' default, not a player's
    host = Workspace(Path("/nonexistent"), {"bluetooth": {"player": PLAYER}})
    assert FakeTorture(FakePlayer(host), Path("x.elf")).pc is None
    assert gen.suite_class("rbfake") is FakeTorture
    sim = engine.scenarios_for(SimSuite(SimPlayer(ws), Path("x.elf")))
    assert all(n.startswith("sim-") for n in sim)


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
    assert rt.parse_status([STATUS_STOPPED]) == rt.Status(0, 0, 0, 0, "-")
    s = rt.parse_status(["bt: media 689 pkts bp 53 behind 0 ms waits 0 restarts 0 bufs 6/9",
                         STATUS_PLAYING])
    assert (s.audio, s.elapsed, s.length, s.hz) == (1, 2469, 249444, 44100)
    assert s.path == "/Eminem/Recovery [Clean Version]/I'm Not Afraid.mp3"
    assert rt.parse_status([STATUS_STARTING]).audio == 6
    assert rt.parse_status([]).audio == -1
    nxt, msgs = rt.parse_msgs(["msgs lost 3", *MSGS])
    assert nxt == 21 and msgs == [
        ("lost", "3"), ("decode", "ea613a3d 161280 /Joel Nielsen - BMS - On a Rail 1.mp3"),
        ("splash", "Bookmark Empty")]
    assert rt.parse_msgs(["err unknown command"]) == (None, [])


def test_progress_across_a_track_change() -> None:
    a = rt.Status(1, 19797, 20021, 48000, "/stress/a.m4a")
    assert rt.progress_ms(a, rt.Status(1, 3193, 20006, 48000, "/stress/b.opus")) == 3417
    assert rt.progress_ms(a, rt.Status(1, 900, 20021, 48000, "/stress/a.m4a")) == 1124
    assert rt.progress_ms(a, rt.Status(1, 19800, 20021, 48000, "/stress/a.m4a")) == 3


def test_message_judging() -> None:
    assert rt.message_problems([("splash", "Bookmark Empty"), ("splash", "No files")]) == []
    assert rt.message_problems([("splash", "No file")]) == ['splash "No file"']
    prompt = ("yesno", "Not enough memory for / the database")
    assert rt.message_problems([prompt], re.compile("(?i)not enough memory")) == []
    assert rt.message_problems([prompt]) == ['yesno "Not enough memory for / the database"']
    # a plugin known to fail without its data, and the splash that says why
    dict_rock = [("splash", "Failed to open index"),
                 ("plugin", "/.rockbox/rocks/apps/dict.rock returned error")]
    assert rt.message_problems(dict_rock) == []
    other = [("plugin", "/.rockbox/rocks/games/rockblox.rock returned error")]
    assert rt.message_problems(other) == [f'plugin "{other[0][1]}"']
    leak = [("plugin", "/.rockbox/rocks/apps/foo.rock leaks file handles")]
    assert len(rt.message_problems(leak)) == 1
    # hashes, video stats and lost counts are judged elsewhere
    assert rt.message_problems([("decode", "deadbeef 1 /failed.mp3"), ("lost", "2")]) == []


def test_decode_hashes_and_video() -> None:
    d = rt.DecodeHashes()
    assert d.add("ea613a3d 161280 /a b.mp3") is None
    assert d.add("ea613a3d 161280 /a b.mp3") is None
    assert d.add("ea613a3d 200000 /a b.mp3") is None          # another length: another key
    assert d.add("00000000 161280 /a b.mp3") == \
        "decode hash 00000000 differs from ea613a3d (/a b.mp3, 161280 frames)"
    assert d.line() == ("Decode hashes: 4 plays of 2 (file, frames), 1 played more than once, "
                        "1 differences")
    v = rt.VideoStats()
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
    assert rt.parse_battery(["bt: x", BATTERY]) == (4293, 100, False)
    assert rt.parse_battery(["battery 4121 mV 69% charging=1 power=1"]) == (4121, 69, True)
    assert rt.ls_entries(LS_ROOT) == ["Bowling for Soup/", "Whistle Remix.mp3", "codectest/",
                                      "yp3dev/"]
    assert rt.ls_entries(["  sd: read error 5 0"]) == []          # a log line in between
    assert [(n.path, n.kind, n.name) for n in rt.parse_menutree(MENUTREE)] == [
        ("8.2", "F", "Running Time"), ("8.3", "F", "Legal Notices"),
        ("8.4", "F", "Debug (Keep Out!)"), ("9", "V", "Shortcuts")]
    assert rt.parse_sel(["sel 5/10 draw 166 | Rockbox | FM Radio"]) == \
        rt.Sel(5, 10, 166, "FM Radio")
    assert rt.parse_sel(["sel 2/10 draw 9 | Rockbox |"]) == rt.Sel(2, 10, 9, "")
    assert rt.parse_sel(["err unknown command"]) is None
    assert rt.parse_bt_status(BT_STATUS) == rt.BtState(3, 1, 968, 2048, "AA:BB:CC:DD:EE:10")
    off = rt.parse_bt_status(["bt stack: status 1 sink 0, irq stack used 1208 of 2048"])
    assert off == rt.BtState(1, 0, 1208, 2048, None)
    assert rt.parse_bt_status(["err unknown command"]) is None
    assert rt.r32_words(["40080068: 10000003", "bt: 12345678: 1"]) == [0x10000003]


def test_no_folder_skip() -> None:
    assert rt.no_folder_skip(["R", "R+", "L", "300", "L+", "S", "R+"]) == \
        ["R", "1500", "R+", "L", "300", "1500", "L+", "S", "R+"]


def test_coverage_file_per_player() -> None:
    ws = Workspace(Path("/w"), {"paths": {"build": "out"}})
    assert rt.coverage_file(FakePlayer(ws)) == Path("/w/out/menu-coverage-rbfake.json")
    ws = Workspace(Path("/w"), {"targets": {"rbfake": {"menu_coverage": "m.json"}}})
    assert rt.coverage_file(FakePlayer(ws)) == Path("/w/m.json")


def test_free_drift() -> None:
    d = gen.FreeDrift(budget=16 * 1024, window=8)
    assert [d.add(100000) for _ in range(8)] == [None] * 8
    assert d.add(60000) is None                  # one dip: the window's ceiling holds
    assert [d.add(90000) for _ in range(7)] == [None] * 7      # within the budget
    lows = [d.add(80000) for _ in range(8)]
    assert lows[:7] == [None] * 7
    assert lows[7] == ("free memory: ceiling 80000 B, 20000 B down over 24 settled readings "
                       "since the boot")
    assert [d.add(80000) for _ in range(8)] == [None] * 8      # measured from there on
    # the playback buffer held (a target that keeps it after a stop): free is
    # what it left, and a leak shrinks it; its readings are a state of their own
    assert [d.add(4000, 2400000) for _ in range(8)] == [None] * 8
    assert [d.add(80000) for _ in range(8)] == [None] * 8      # released again
    assert [d.add(4000, 2380000) for _ in range(7)] == [None] * 7
    assert d.add(4000, 2380000) == ("free memory and the held playback buffer: ceiling "
                                    "2384000 B, 20000 B down over 16 settled readings "
                                    "since the boot")
    d.reset()
    assert not d.reference and not d.readings


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


def test_weight_zero() -> None:
    """A weight of 0 leaves a scenario out of the steps and of the run's
    first line"""
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        _, _, steps = sim_run(Path(tmp), ["sim-ok", "sim-fail"], 8, weights={"sim-fail": 0},
                              echo=lines.append)
    assert [x.name for x in steps] == ["sim-ok"] * 8
    assert lines[0].endswith(", scenarios sim-ok")


def test_simulated_leak() -> None:
    """Free memory read after settled steps: a slow leak fails the step where
    the ceiling falls past the budget, once"""
    with tempfile.TemporaryDirectory() as tmp:
        _, _, steps = sim_run(Path(tmp), ["sim-leak"], 16)
    failed = [x for x in steps if not x.ok]
    # 3000 B a step: the first 8 readings make the reference (97000); the
    # ceiling of the last 8 (79000 at reading 14) is the first over 16 KiB below
    assert [x.step for x in failed] == [14]
    assert failed[0].problems[0].startswith("free memory: ceiling 79000 B, 18000 B down")


def test_keys_fit_the_shell_line() -> None:
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (p, s, _):
        script = " ".join(random.Random(4).choices(rt.MONKEY_KEYS, rt.MONKEY_WEIGHTS, k=60))
        p.dev.sent.clear()
        s.keys(script)
        assert len(p.dev.sent) > 1 and all(x.startswith("keys ") for x in p.dev.sent)
        assert " ".join(x.removeprefix("keys ") for x in p.dev.sent) == script


class SelList:
    """A menu as `sel` and `keys` see it: Downs and Ups move the selection
    (wrapping) unless the UI drops them (the Nth presses in DROP, counted
    from 1), Select opens the next level with its first item selected"""

    def __init__(self, drop: set[int], start: int = 0, count: int = 10,
                 names: list[str] | None = None) -> None:
        self.drop, self.at, self.count = drop, start, count
        self.names = names or [f"item {i}" for i in range(count)]
        self.presses = self.draws = 0
        self.selected: list[int] = []

    def sh(self, line: str, timeout: float = 120.0) -> list[str]:
        if line == "health":
            return ["health up 30 s audio 0 elapsed 0/0 underruns 0/0 stalls 0 maxstall 0 "
                    "ui_late 0 ui_age 6 free 18912 cpu 64 act 3 plugin - db 0"]
        assert line == "sel", line
        return [f"sel {self.at}/{self.count} draw {self.draws} | Menu | {self.names[self.at]}"]

    def keys(self, script: str) -> None:
        for k in script.split():
            if k in ("D", "U"):
                self.presses += 1
                if self.presses not in self.drop:
                    self.at = (self.at + (1 if k == "D" else -1)) % self.count
                    self.draws += 1
            elif k == "S":
                self.selected.append(self.at)
                self.at = 0
                self.draws += 1


def test_walk_confirms_each_level() -> None:
    """A Down the UI dropped is pressed again before Select; a main menu
    entry starts wherever the selection was; a build without `sel` gets the
    keys blind"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (p, s, _):
        s.tree = []
        ui = SelList(drop={2, 4})
        with mock.patch.object(s, "sh", ui.sh), mock.patch.object(s, "keys", ui.keys):
            s.walk_to("3.2.0.1", 2000)
        assert ui.selected == [3, 2, 0, 1]
        ui = SelList(drop=set(), start=2)
        with mock.patch.object(s, "sh", ui.sh), mock.patch.object(s, "keys", ui.keys), \
                mock.patch.object(s, "home", return_value=True):
            s.menu("recording")
        assert ui.selected == [s.main_menu.index("recording")]
        sent = len(p.dev.sent)
        s.walk_to("1.0", 2000)
        assert [x for x in p.dev.sent[sent:] if x.startswith(("keys", "sel"))] == \
            ["keys D 150", "sel", "keys S 1500", "sel", "keys S 2000"]


def test_walk_finds_an_item_the_tree_left_out() -> None:
    """The tree has no File View (its callback hides it outside Settings):
    the walk to System at tree index 3 finds it one further down; an item
    whose name is in no list gets a note and its index"""
    names = ["While Playing Screen", "Playlists", "File View", "Database", "Display", "System"]
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (_, s, _):
        tree = [n for n in names if n != "File View"]
        s.tree = [rt.MenuNode("0", "M", "Settings")] + [
            rt.MenuNode(f"0.{i}", "M", n) for i, n in enumerate(tree)]
        ui = SelList(drop=set(), count=len(names), names=names)
        with mock.patch.object(s, "sh", ui.sh), mock.patch.object(s, "keys", ui.keys):
            s.walk_to("0.4", 2000)
            assert ui.selected == [0, names.index("System")]
            s.tree[-1] = rt.MenuNode("0.4", "M", "Bluetooth: Off")
            s.walk_to("0.4", 2000)
        assert ui.selected[2:] == [0, 4]
        assert "'Bluetooth: Off' (menu tree 0.4) is not in the list" in s.notes[-1]


def test_database_refusals_are_not_failures() -> None:
    """Database stays shut when music holds the memory: the low memory
    yesno, or the commit tagcache delays to the next boot; anything else
    that leaves the main menu is a failure"""
    cases = [([("yesno", "Not enough memory for the database. Continue?")], "not enough memory"),
             ([("splash", rt.DB_COMMIT_DELAYED)], "waits for a reboot"),
             ([("splash", "No files")], None)]
    for msgs, why in cases:
        with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (_, s, _), \
                mock.patch.object(s, "music"), mock.patch.object(s, "home", return_value=True), \
                mock.patch.object(s, "menu", return_value=rt.ACT_MAINMENU), \
                mock.patch.object(s, "msgs", return_value=msgs), \
                mock.patch.object(s, "menu_fail") as fail:
            got = rt.dbdeep(s)
            if why:
                assert got and why in got and not fail.called
            else:
                assert got is None and "gave activity" in fail.call_args.args[0]


def test_plugin_keys_stop_when_the_plugin_quits() -> None:
    """Random keys go to the plugin only: none after it quit"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (p, s, _):
        sent = len(p.dev.sent)
        with mock.patch.object(s, "in_plugin", side_effect=[True, True, False]):
            s.plugin_keys(["L", "S", "D", "S"])
        assert [x for x in p.dev.sent[sent:] if x.startswith("keys")] == ["keys L", "keys S"]


def test_close_plugin_stops_mid_chord() -> None:
    """An exit chord that quits the plugin halfway: the rest of it is not
    sent (it would work the main menu), nor any later exit"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (p, s, _):
        sent = len(p.dev.sent)
        first = rt.PLUGIN_EXITS[0].split()
        quits_after = len(first) + 1
        with mock.patch.object(s, "in_plugin",
                               side_effect=[True] * quits_after + [False]):
            assert s.close_plugin()
        keys = [x for x in p.dev.sent[sent:] if x.startswith("keys")]
        assert keys == [f"keys {k}" for k in (first + rt.PLUGIN_EXITS[1].split())[:quits_after]]


def test_library_track_backs_out_of_dead_ends() -> None:
    """A stock card's folders hold no music: the walk backs out of them and
    finds the album; no library at all: None, within LIBRARY_LS listings"""
    def card(tree: dict[str, list[str]]) -> Callable[..., list[str]]:
        def sh(line: str, **_: object) -> list[str]:
            listed.append(line)
            folder = line.removeprefix("ls ")
            return [f"ls {folder}:", *(f"  {e} 0" for e in tree.get(folder, []))]
        return sh

    stock = {"/": ["Picture/", "Video/", "Recordings/", "codectest/", "rbtest/", "Artist/",
                   "Single.mp3"],
             "/Picture": ["./", "../", "001.jpg"], "/Video": ["clip.avi", "Old/"],
             "/Video/Old": ["a.avi"], "/Recordings": [], "/codectest": ["mp3_320.mp3"],
             "/rbtest": ["tone.mp3"], "/Artist": ["Notes/", "Album/"],
             "/Artist/Notes": ["x.txt"], "/Artist/Album": ["01 song.flac", "cover.jpg"]}
    listed: list[str] = []
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (_, s, _):
        with mock.patch.object(s, "sh", card(stock)):
            for seed in range(20):
                s.rng = random.Random(seed)
                assert s.library_track(album=True) == "/Artist/Album/01 song.flac", seed
                assert s.library_track() == "/Single.mp3", seed
        assert "ls /codectest" not in listed and "ls /rbtest" not in listed
        assert "ls /Video/Old" in listed                    # a dead end two folders down
        bare = {"/": [f"D{i}/" for i in range(30)], **{f"/D{i}": ["x/", "y/"] for i in range(30)}}
        listed.clear()
        with mock.patch.object(s, "sh", card(bare)):
            assert s.library_track() is None
        assert len(listed) == rt.LIBRARY_LS


def test_boot_and_steps() -> None:
    """A new player's suite with RockboxTorture's defaults: the settings files
    on its card over mass storage once, before the boot with the shell;
    steps, and what every step is checked for"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (p, s, _):
        assert p.storage_boots == [Path(tmp) / "rockbox.elf"]
        assert sorted(f.name for f in (p.card / "rbtest").iterdir()) == sorted(rt.CFGS)
        assert (p.card / "rbtest" / "t_wake.cfg").read_text() == rt.CFGS["t_wake.cfg"]
        (req,) = p.requests
        assert (req.seconds, req.usb, req.settings) == (0, True, {})
        assert f"play {rt.PROBE}" in p.dev.sent and not any("t_limit" in x for x in p.dev.sent)
        # the run's light look from the boot on, before crawl saves its settings
        assert "cfg /rbtest/t_default.cfg" in p.dev.sent
        assert (s.tone, s.theme_cfgs[-1]) == ("/rbtest/tone.mp3", "/rbtest/t_default.cfg")
        assert s.library_track(album=True) is None      # codectest/ and rbtest/ are no albums
        for name in ("codecs", "decode", "meta", "fs"):
            assert step(s, name) == [], name
        assert any(x.startswith("mkcheck /torture") for x in p.dev.sent)
        # what every step is checked for
        p.dev.msgs.append("splash Error accessing playlist control file")
        o = step_outcome(s, "meta")
        assert o.problems == ['messages: splash "Error accessing playlist control file"']
        # the step file has the messages judged
        after = o.output.split("\nAfter the step:\n", 1)[1]
        assert "msg 1 splash Error accessing playlist control file" in after
        p.dev.msgs.append("decode ea613a3d 161280 /codectest/mp3_320.mp3")
        assert step(s, "meta") == []
        p.dev.msgs.append("decode 0badf00d 161280 /codectest/mp3_320.mp3")
        assert step(s, "meta") == ["messages: decode hash 0badf00d differs from ea613a3d "
                                   "(/codectest/mp3_320.mp3, 161280 frames)"]
        p.dev.charging, p.dev.level = True, 85          # no charge limit: a reading only
        assert step(s, "meta") == []
        p.dev.glitch_on = "mkfiles"
        assert step(s, "fs") == ["underruns +1"]
        p.dev.glitch_on = ""
        # a play of a missing file stops; one that sits in "playing" fails
        assert step(s, "missing") == []
        p.dev.stuck_on_missing = True
        (problem,) = step(s, "missing")
        assert problem.startswith("still playing (/rbtest/missing-") and "a missing file" in problem
        p.dev.stuck_on_missing = False
        assert s.tally == {"codecs": [1, 0], "decode": [1, 0], "meta": [5, 2], "fs": [2, 1],
                           "missing": [2, 1]}
        # a recording from the microphone: the shared shell tunes only with its screen
        step(s, "record", seed=3)
        assert any(x.startswith("rec ") and " mic mono " in x for x in p.dev.sent)
        assert not any(x.startswith("fm") for x in p.dev.sent)


def test_sink_left_on_bluetooth() -> None:
    """A step that leaves the output on Bluetooth with no device linked
    fails, and the output goes back to the built-in one"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, _):
        assert step(s, "meta") == []                    # linked to this PC: fine
        p.dev.bt_addr, p.dev.sink = None, 1
        assert step(s, "meta") == ["output left on Bluetooth with no device linked "
                                   "(set back to the built-in one)"]
        assert p.dev.sink == 0
        assert step(s, "meta") == []


def test_bt_setup_after_a_run_cut_short() -> None:
    """The first bt step after a run was cut short in a Bumble step: the
    adapter reclaimed, and the player, which kept this PC's key that BlueZ
    forgot, paired again at once (no connects that cannot get through)"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, pc):
        assert pc is not None
        s.bt_known, pc.knows, pc.reclaimed = False, False, 0     # a new run
        p.dev.drop()
        sent = len(p.dev.sent)
        assert step(s, "bt-churn") == []
        assert pc.reclaimed == 1 and pc.agents == 1 and pc.knows and p.dev.bt_addr == PC
        connects = [x for x in p.dev.sent[sent:] if x.startswith("bt stack connect")]
        assert len(connects) == 2, connects         # the pairing's, churn's own
        # the music repeats while the step works the stream, and not after
        new = p.dev.sent[sent:]
        on, off = new.index("repeat 1"), len(new) - 1 - new[::-1].index("repeat 0")
        at = [i for i, x in enumerate(new) if x.startswith("bt stack connect")]
        assert on < at[-1] < off, new


def test_bt_forget_and_pair_again() -> None:
    """bt-forget: the player forgets this PC and pairs again through the
    agent; a setup that finds both keys gone (a Bumble peer's step cut
    short) pairs through it too"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, pc):
        assert pc is not None
        assert step(s, "bt-forget") == []
        assert f"bt stack forget {PC}" in p.dev.sent and pc.agents == 1
        assert p.dev.bt_paired and p.dev.bt_addr == PC
        p.dev.bt_paired, pc.knows = False, False
        p.dev.drop()
        assert step(s, "bt-churn") == []
        assert pc.agents == 2 and p.dev.bt_addr == PC
        # nothing answers its page: the setup fails, both sides tried, and
        # again after a USB reset of this PC's adapter
        p.dev.bt_paired, pc.knows = False, False
        p.dev.drop()
        with mock.patch.object(FakePcLink, "answers", return_value=False):
            problems = step(s, "bt-churn")
        assert len(problems) == 1 and problems[0].startswith(
            "bt setup: no connection from either side within 25 s"), problems
        assert "paired False" in problems[0] and pc.agents == 6 and pc.usb_resets == 1
        (fail, _) = sorted(Path(tmp).glob("*-bt-fail*.txt"), key=lambda f: len(f.name))
        text = fail.read_text()
        assert "--- bluetoothctl connect" in text and "--- sh log" in text
        assert "lmp" not in text and "link table" not in text   # a player's own traces only


def test_bt_setup_usb_reset() -> None:
    """Links that come up with no data, this PC's adapter's fault
    (2026-11-05): the setup USB-resets the adapter once, and the player
    then connects"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, pc):
        assert pc is not None
        p.dev.drop()
        pc.dead = True
        assert step(s, "bt-churn") == []
        assert pc.usb_resets == 1 and p.dev.bt_addr == PC


def test_bt_capture() -> None:
    """bt-capture: the tone's loud part as this PC records it"""
    def record(gap: tuple[float, float] | None) -> Callable[[Path, str, float], None]:
        def fake(wav: Path, sink: str, seconds: float) -> None:
            assert sink == "arb_null" and rt.CAPTURE_S[0] <= seconds <= rt.CAPTURE_S[1]
            tone_wav(wav, seconds, gap)
        return fake

    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, _):
        with mock.patch.object(rt.pcsink, "record", record(None)):
            assert step(s, "bt-capture") == []
        assert "play /rbtest/tone.mp3" in p.dev.sent
        with mock.patch.object(rt.pcsink, "record", record((2.0, 0.03))):
            problems = step(s, "bt-capture")
        assert len(problems) == 1 and "1 dropouts (s, ms): [(2.0, 29)]" in problems[0], problems


def test_bt_rate() -> None:
    """bt-rate: the 48 kHz files queued on the built-in output while the
    link is down must play on when it comes back (the stream at 44.1 kHz on
    a build without `status full`'s freq), then a 44.1 kHz file plays"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, _):
        sent = len(p.dev.sent)
        assert step(s, "bt-rate") == []
        plays = [x for x in p.dev.sent[sent:] if x.startswith("play /stress/")]
        assert len(plays) == 2, plays
        assert sorted(plays[0][5:].split(";")) == ["/stress/flac_16_48.flac",
                                                   "/stress/mp3_cbr192_48k.mp3"]
        assert plays[1] in ("play /stress/wav_16_44.wav", "play /stress/aiff_16_44.aiff")
        assert p.dev.bt_addr == PC
        # a build that remade the buffer under them: playback stopped
        p.dev.stops_on_rate = True
        problems = step(s, "bt-rate")
        assert len(problems) == 1 and problems[0].endswith(
            "audio 0 after the output moved to the headset"), problems


def test_capture_problems() -> None:
    good = analysis.WavAnalysis(44100, 2, 5.0, [1000.2, 1499.0], [(1.0, 4.0)], [])
    assert rt.capture_problems(good) == []
    mono = analysis.WavAnalysis(44100, 2, 2.5, [1000.0, None], [(1.0, 12.0)], [])
    assert rt.capture_problems(mono) == [
        "channel 1: tone None Hz, want 1500", "only 2.5 s recorded",
        "1 dropouts (s, ms): [(1.0, 12)]"]


def test_hid_keys() -> None:
    assert set(rt.HID_KEYS) <= set(keys_for("keyboard"))
    assert set(rt.HID_KEYS + rt.HID_DS4_KEYS) <= set(keys_for("ds4"))
    assert "a" not in rt.HID_KEYS + rt.HID_DS4_KEYS


def test_bt_headset() -> None:
    """bt-headset: the adapter lent (both keys gone), the cycles judged from
    cycles.txt, the adapter back with BlueZ and the player paired again"""
    if importlib.util.find_spec("bumble") is None:
        raise Skip("needs Bumble (uv sync --extra bt)")
    from auto_rockbox.btpeer import pcheadset  # noqa: PLC0415 (Bumble)

    def cycles(opts: rt.HeadsetOptions, *, limit: float, echo: bool) -> None:
        assert opts.adapter == "usb:10d7:b012" and opts.player == PLAYER and not echo
        assert not p.dev.bt_paired and not pc.knows and p.dev.bt_addr is None
        assert limit == opts.cycles * rt.HEADSET_CYCLE_S + rt.HEADSET_SPARE_S
        opts.out.mkdir()
        lines = [f"2026-11-05 12:00:0{n} cycle {n}: ok; page:0.5 media:2.1"
                 for n in range(1, opts.cycles)]
        lines.append(f"2026-11-05 12:00:09 cycle {opts.cycles}: stall at media; page:0.5; "
                     "vanished")
        (opts.out / "cycles.txt").write_text("\n".join(lines) + "\n")
        p.dev.mirrored += ["bt: connected, handle 11", "bt: disconnected: 08"]
        p.dev.stalls += 1                   # playback, until the vanished link's timeout
        while p.dev.mirrored:               # the mirror reads them meanwhile
            threading.Event().wait(0.01)

    with (tempfile.TemporaryDirectory() as tmp,
          rockbox_run(Path(tmp), "usb:10d7:b012") as (p, s, link),
          mock.patch.object(pcheadset, "main", cycles)):
        assert link is not None
        pc = link
        problems = step(s, "bt-headset")       # the vanished cycle's stall is no problem
        assert len(problems) == 1 and problems[0].endswith(
            " ok, 1 lost: cycle 7: stall at media; page:0.5; vanished"), problems
        # reclaimed by the run's first bt step and after the peer
        assert pc.reclaimed == 2 and pc.agents == 1 and p.dev.bt_paired and p.dev.bt_addr == PC
        # the player rescans before it pages this PC again
        last = max(i for i, x in enumerate(p.dev.sent) if x == f"bt stack connect {PC}")
        assert "bt stack scan" in p.dev.sent[:last]
        assert "repeat 1" in p.dev.sent and "repeat 0" in p.dev.sent
        assert [x.split(" | ", 1)[1] for x in s.transcript if " | " in x] == [
            "bt: connected, handle 11", "bt: disconnected: 08"]


def test_linein_waits_out_the_auto_connect() -> None:
    """Bluetooth on with no link yet (a boot just before): the auto-connect's
    link comes up before the tone, which then plays to the built-in output"""
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp), "") as (p, s, pc):
        assert pc is not None
        capture = Path(tmp) / "linein"
        capture.mkdir()
        (capture / "latest.json").write_text("{}")
        p.dev.drop()
        polls, bt = 0, p.dev.bt

        def auto_connect(line: str) -> list[str]:
            nonlocal polls
            if line == "status" and p.dev.bt_addr is None:
                polls += 1
                if polls == 3:
                    p.dev.bt_addr, p.dev.sink = PC, 1
            return bt(line)
        sent = len(p.dev.sent)
        with (mock.patch.object(p.dev, "bt", auto_connect),
              mock.patch.object(rt.linein, "directory", return_value=capture),
              mock.patch.object(rt.linein, "tone_check",
                                return_value=rt.linein.ToneCheck(heard=[], need=0))):
            assert step(s, "linein") == []
        cmds = p.dev.sent[sent:]
        play = next(i for i, x in enumerate(cmds) if x.startswith("play"))
        assert "bt stack sink builtin" in cmds[:play] and "bt stack sink bt" in cmds[play:]
