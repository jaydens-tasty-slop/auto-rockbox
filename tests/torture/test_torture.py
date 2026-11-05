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
    tone_wav,
)

PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
LEDGER = PORT / "docs/torture-runs.csv"

# Every player's scenarios, and those that need a feature of the player's build
SHARED_WEIGHTS = {"codecs": 12, "skips": 6, "plugins": 6, "fs": 4, "db": 4, "usb": 2,
                  "monkey": 8, "menus": 6, "dbdeep": 4, "crawl": 16, "recui": 4, "fmui": 4,
                  "longplay": 4, "record": 8, "sdodd": 4, "meta": 4, "theme": 4, "codecmix": 8,
                  "shuffle": 8, "folders": 4, "decode": 10, "linein": 4, "video": 4}
FEATURE_WEIGHTS = {"sleep": 4}
BT_WEIGHTS = {"bt-onoff": 3, "bt-pcdisc": 2, "bt-pcpower": 0, "bt-avrcp": 2, "bt-churn": 2,
              "bt-disturb": 2, "bt-reboot": 1, "bt-forget": 1, "bt-capture": 2}
BUMBLE_WEIGHTS = {"bt-headset": 2, "bt-hid": 2}

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


def test_message_judging() -> None:
    assert rt.message_problems([("splash", "Bookmark Empty")]) == []
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


def test_keys_fit_the_shell_line() -> None:
    with tempfile.TemporaryDirectory() as tmp, rockbox_run(Path(tmp)) as (p, s, _):
        script = " ".join(random.Random(4).choices(rt.MONKEY_KEYS, rt.MONKEY_WEIGHTS, k=60))
        p.dev.sent.clear()
        s.keys(script)
        assert len(p.dev.sent) > 1 and all(x.startswith("keys ") for x in p.dev.sent)
        assert " ".join(x.removeprefix("keys ") for x in p.dev.sent) == script


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
        assert (s.tone, s.theme_cfgs[-1]) == ("/rbtest/tone.mp3", "/rbtest/t_default.cfg")
        assert s.library_track(album=True) is None      # codectest/ and rbtest/ are no albums
        for name in ("codecs", "decode", "meta", "fs"):
            assert step(s, name) == [], name
        assert any(x.startswith("mkcheck /torture") for x in p.dev.sent)
        # what every step is checked for
        p.dev.msgs.append("splash Error accessing playlist control file")
        assert step(s, "meta") == ['messages: splash "Error accessing playlist control file"']
        p.dev.msgs.append("decode ea613a3d 161280 /codectest/mp3_320.mp3")
        assert step(s, "meta") == []
        p.dev.msgs.append("decode 0badf00d 161280 /codectest/mp3_320.mp3")
        assert step(s, "meta") == ["messages: decode hash 0badf00d differs from ea613a3d "
                                   "(/codectest/mp3_320.mp3, 161280 frames)"]
        p.dev.charging, p.dev.level = True, 85          # no charge limit: a reading only
        assert step(s, "meta") == []
        p.dev.glitch_on = "mkfiles"
        assert step(s, "fs") == ["underruns +1"]
        assert s.tally == {"codecs": [1, 0], "decode": [1, 0], "meta": [5, 2], "fs": [2, 1]}
        # a recording from the microphone: the shared shell tunes only with its screen
        step(s, "record", seed=3)
        assert any(x.startswith("rec ") and " mic mono " in x for x in p.dev.sent)
        assert not any(x.startswith("fm") for x in p.dev.sent)


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
        # nothing answers its page: the setup fails, both sides tried
        p.dev.bt_paired, pc.knows = False, False
        p.dev.drop()
        with mock.patch.object(FakePcLink, "answers", return_value=False):
            problems = step(s, "bt-churn")
        assert len(problems) == 1 and problems[0].startswith(
            "bt setup: no connection from either side within 25 s"), problems
        assert "paired False" in problems[0] and pc.agents == 4
        (fail,) = Path(tmp).glob("*-bt-fail.txt")
        text = fail.read_text()
        assert "--- bluetoothctl connect" in text and "--- sh log" in text
        assert "lmp" not in text and "link table" not in text   # a player's own traces only


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

    with (tempfile.TemporaryDirectory() as tmp,
          rockbox_run(Path(tmp), "usb:10d7:b012") as (p, s, link),
          mock.patch.object(pcheadset, "main", cycles)):
        assert link is not None
        pc = link
        problems = step(s, "bt-headset")
        assert len(problems) == 1 and problems[0].endswith(
            " ok, 1 lost: cycle 7: stall at media; page:0.5; vanished"), problems
        assert pc.reclaimed == 1 and pc.agents == 1 and p.dev.bt_paired and p.dev.bt_addr == PC
        assert "repeat 1" in p.dev.sent and "repeat 0" in p.dev.sent
