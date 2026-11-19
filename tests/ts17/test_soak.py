"""The TS17 soak scenarios offline: registration, judging real samples (the
logs and Line In seconds of ts17dev's soak run "soak7" on 2026-11-05, and
the two pitfalls of the runs before it), and steps through the engine with
a fake player whose launches return at once."""

from __future__ import annotations

import json
import random
import tempfile
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest import mock

from auto_rockbox import engine, linein, media
from auto_rockbox.engine import Outcome, StepContext
from auto_rockbox.lock import DeviceLock
from auto_rockbox.suites.soak import SoakSuite, boot
from auto_rockbox.target import RunRequest
from auto_rockbox.targets.ts17 import Ts17, soak
from auto_rockbox.targets.yp3 import Yp3
from auto_rockbox.workspace import Workspace

WEIGHTS = {"tone": 2, "codec": 5, "video": 3, "wps": 2, "browse": 2, "fm": 1, "rec": 2,
           "boot": 2, "usb": 2, "alarm": 1, "poweroff": 1}

# ---------------------------------------------------------------- samples

# A boot log as the dev log ring holds it (soak7 step 21, `tone`); the
# return line and byte count are filled in per run
HEAD = "Rockbox 3b310b925dM-261105 on TS17; return after {seconds} s"
BODY = """battery ADC offset -16 raw
rtc: boot ALARM8 00000000 COUNT8 04778e47 WAKE_PD 00180001 WAKE 00000111 INTMASK 00000005
keys 00 (ladder 319c)
sd: v2 1 hs 1 ocr c0ff8000 cid 05000c61 73646667 22000000 1201a47f
sd: csd 400e0032 5b590000 ea617f80 0a4040b9, 61442048 blocks
rtc: state 1; RTC lost 0, counter kept 1, alarm woke 0; alarm 9:15
dac: powered up, 0 timeouts
bt: controller up, ACL 6 x 1021
bt: connection failed: 04
keys: done
t 30 s: battery 4222 mV, 100%; 2026-11-05 10:12:32; 96 MHz, phones 1"""
TAIL = "returning to download mode at tick {tick}\n-- 635 bytes logged\n"

# Step 7 (`video`, 04_mjpeg.avi): the Video Player's dev stats
VIDEO_LINES = """video: pics t=10 shown=248 drop=0 late=0/6 dec=19.9/20 lcd=0.0 idle=50
video: sound t=10 under=0/0 low=139/185 cpu=0 read=10/0 lat=0+0
video: pics end=20 shown=499 drop=0 late=0/6 dec=19.7/20 lcd=0.0 idle=50
video: sound end=20 under=0/0 low=139/185 cpu=0 read=10/0 lat=0+0"""

# Step 18 (`alarm`): the alarm set and firing while on
ALARM_LINES = """rtc: alarm in 209 s at count 7833715 (tick after 105400 us); ALARM8 00778873
rtc: alarm while on; ALARM8 04778873, now 00778873 COUNT8 04778874"""
ALARM_SCRIPT = ("9000 D D D S 1500 D D D D D D S 2000 D S 1500 U R D D D D D D S 3000")

# soak6-po step 1 (`poweroff`): the boot the alarm woke, with no host config
WAKE_LOG = """Rockbox 3b310b925dM-261105 on TS17; return after 90 s (no host config)
battery ADC offset -16 raw
rtc: boot ALARM8 0476f1d3 COUNT8 0476f1d6 WAKE_PD 00180011 WAKE 00000111 INTMASK 00000005
keys 00 (ladder 31a4)
rtc: state 1; RTC lost 1, counter kept 1, alarm woke 1; alarm 8:51
rtc: restored 157 s later at rate 8283000
dac: powered up, 0 timeouts
bt: connection failed: 04
returning to download mode at tick 9000
-- 778 bytes logged
"""

# Line In seconds as the runs listed them: stamp, left and right (dB, Hz,
# purity %), ms low. QUIET marks a second the listing left out (no tone).
Row = tuple[str, float, float, int, float, float, int, int]
QUIET = (-80.0, 0.0, 0, -80.0, 0.0, 0, 0)


def tone_rows(stamps: list[str], first_low: int, last: tuple[float, float, int, int],
              level: tuple[float, float] = (-50.6, -50.8)) -> list[Row]:
    """A run of the test tone: a partial first second (FIRST_LOW ms low),
    steady ones at LEVEL, and the step-down's second LAST (dB L, dB R, %, %)"""
    rows: list[Row] = [(stamps[0], level[0] - 0.8, 1000.5, 99, level[1] - 0.8, 1500.5, 99,
                        first_low)]
    rows += [(s, level[0], 1000.5, 99, level[1], 1500.5, 99, 0) for s in stamps[1:-1]]
    rows.append((stamps[-1], last[0], 1000.5, last[2], last[1], 1500.5, last[3], 0))
    return rows


# soak7 step 21 (`tone`): 11 seconds with tone (the last partial, 6.4 dB down), two published
# in wall second :24
TONE_021: list[Row] = [
    ("17:12:24.274", *QUIET), ("17:12:24.956", *QUIET),
    *tone_rows(["17:12:25.980", "17:12:27.004", "17:12:28.028", "17:12:29.052",
                "17:12:30.076", "17:12:31.100", "17:12:32.125", "17:12:33.149",
                "17:12:34.173", "17:12:35.196", "17:12:36.221"], 175, (-57.0, -57.2, 89, 88)),
    ("17:12:37.244", *QUIET),
]

# soak6 step 35 (`tone`), stamped to the second: the tone starts in the first
# of two seconds published at 16:39:42, and the old checker took that
# partial second's 192 ms low for a dropout in the whole one
TONE_SOAK6: list[Row] = [
    ("16:39:41", *QUIET),
    *tone_rows(["16:39:42", "16:39:42", "16:39:44", "16:39:45", "16:39:46", "16:39:47",
                "16:39:48", "16:39:49", "16:39:50", "16:39:51", "16:39:52"],
               192, (-56.8, -56.9, 90, 90)),
    ("16:39:53", *QUIET),
]

# soak7 step 33 (`codec`, 19_adx.adx): its stereo tone, then the next
# clip, 20_smaf.mmf, mono 1 kHz on both sides at the purity threshold
CODEC_033: list[Row] = [
    ("17:23:48.985", *QUIET),
    *tone_rows(["17:23:50.009", "17:23:51.033", "17:23:52.057", "17:23:53.081",
                "17:23:54.105", "17:23:55.129", "17:23:56.153", "17:23:57.177",
                "17:23:58.201", "17:23:59.225", "17:24:00.249"], 40, (-59.1, -59.6, 93, 92),
               level=(-49.9, -50.2)),
    ("17:24:01.273", *QUIET),
    ("17:24:01.956", -53.7, 1000.2, 49, -53.9, 1000.2, 49, 114),
    ("17:24:02.980", -53.2, 1000.2, 50, -53.4, 1000.2, 50, 0),
    ("17:24:04.004", -53.2, 1000.2, 50, -53.4, 1000.2, 50, 0),
    ("17:24:05.028", -53.2, 1000.2, 50, -53.4, 1000.2, 50, 9),
    ("17:24:06.052", -53.2, 1000.2, 51, -53.4, 1000.2, 51, 0),
]


def heard(rows: list[Row]) -> list[linein.Heard]:
    out = []
    for seq, (stamp, ldb, lhz, lpct, rdb, rhz, rpct, low) in enumerate(rows, 1):
        when = datetime.fromisoformat(f"2026-11-05T{stamp}+00:00")
        out.append(linein.Heard(linein.Second(seq, when, Path("-")),
                                (linein.Channel(ldb, lhz, lpct / 100),
                                 linein.Channel(rdb, rhz, rpct / 100)),
                                ((0, low),) if low else ()))
    return out


def entry(path: str, kind: str, step: float | None = 10.0) -> media.Entry:
    return media.Entry(path, kind, 12.0, step if kind in soak.TONE_KINDS else None)


# The card's set as soak7 had it: 41 codec clips, 6 videos
CODEC_NAMES = ["mp3_192.mp3", "mp2_192.mp2", "aac_128.m4a", "aac_adts.aac", "alac.m4a",
               "flac.flac", "wav_pcm.wav", "wav_ima.wav", "wave64.w64", "aiff.aiff", "au.au",
               "tta.tta", "wavpack.wv", "vorbis_q5.ogg", "opus_128.opus", "speex_wb.spx",
               "ac3_192.ac3", "wma_128.wma", "adx.adx", "smaf.mmf", "vox_8k.vox", "mod.mod"]
CARD_SET = [
    *(entry(f"codecs/{n:02d}_{name}", "music" if name == "mod.mod" else
            "tone" if name in ("speex_wb.spx", "smaf.mmf", "vox_8k.vox") else "stereo")
      for n, name in enumerate(CODEC_NAMES, 1)),
    *(entry(f"codecs/{n:02d}_hires.flac", "music") for n in range(23, 40)),
    entry("codecs/40_wv_16_44.wv", "music"), entry("codecs/41_wv_24_96.wv", "tone", None),
    *(entry(f"video/{n:02d}_{name}", "stereo")
      for n, name in enumerate(["mpeg1.mpg", "mpeg2.mpg", "mpeg1.mkv", "mjpeg.avi"], 1)),
    entry("video/05_stock.avi", "music"), entry("video/06_land_ima.avi", "music"),
]

# ---------------------------------------------------------------- a fake player


def normal_log(req: RunRequest, extra: str = VIDEO_LINES + "\n" + ALARM_LINES) -> str:
    """A boot log for REQ: soak7's lines, EXTRA, the return line"""
    keys = f"keys: {req.keys}\n" if req.keys else ""
    return (f"{HEAD.format(seconds=req.seconds)}\n{keys}{BODY}\n{extra}\n"
            f"{TAIL.format(tick=req.seconds * 100)}")


def any_log(req: RunRequest) -> str:
    return WAKE_LOG if "P+++" in req.keys else normal_log(req)


class FakeTs17(Ts17):
    """The TS17 with no transport: a launch is noted, the image is back at
    once, and its log is RING(request)"""

    def __init__(self, ws: Workspace, ring: Callable[[RunRequest], str] = any_log) -> None:
        super().__init__(ws)
        self.ring = ring
        self.requests: list[RunRequest] = []

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def before_run(self, req: RunRequest) -> None:
        return None

    def launch(self, req: RunRequest) -> None:
        self.requests.append(req)

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        tick()
        return True

    def read_log(self, elf: Path | None = None) -> str:
        return self.ring(self.requests[-1])

    def after_run(self, req: RunRequest, returned: bool) -> None:
        return None


class Pick(random.Random):
    """A random source whose randrange is PICK: the scenario takes that file"""

    def __init__(self, pick: int) -> None:
        super().__init__(1)
        self.pick = pick

    def randrange(self, start: int, stop: int | None = None, step: int = 1) -> int:
        return self.pick


def workspace(root: Path, files: list[media.Entry]) -> Workspace:
    (root / "media").mkdir()
    (root / "media" / "manifest.json").write_text(json.dumps(
        {"files": [asdict(e) for e in files]}))
    return Workspace(root, {"targets": {"ts17": {"soak": {
        "media": "media", "folder": "/ts17test/", "row": 5, "tone": "tone.mp3"}}}})


def step(t: Ts17, name: str, rng: random.Random | None = None) -> StepContext:
    suite = SoakSuite(t, Path("rockbox.elf"), shots=False)
    return StepContext(1, name, t, suite, rng or random.Random(1), t.ws.root, datetime.now())


class LineIn:
    """linein.tone_check, judging ROWS instead of the ring; notes its calls"""

    def __init__(self, rows: list[Row]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    def __call__(self, directory: Path, since: datetime, until: datetime, *, need: int = 6,
                 stereo: bool = True, span: int | None = None,
                 lag: float = linein.LAG_S) -> linein.ToneCheck:
        self.calls.append({"need": need, "stereo": stereo, "span": span})
        return linein.judge(heard(self.rows), need=need, stereo=stereo, span=span)


# ---------------------------------------------------------------- tests

def test_registered_for_ts17_only() -> None:
    ts17 = engine.scenarios_for(SoakSuite(Ts17(Workspace(Path("/nonexistent"))),
                                          Path("x.elf"), shots=False))
    assert {n: s.weight for n, s in ts17.items() if s.targets == {"ts17"}} == WEIGHTS
    yp3 = engine.scenarios_for(SoakSuite(Yp3(Workspace(Path("/nonexistent"))),
                                         Path("x.elf"), shots=False))
    assert not [n for n, s in yp3.items() if s.targets and "ts17" in s.targets]
    assert ts17["boot"].needs == {"cold"} and ts17["usb"].needs == {"usb", "storage"}


def test_line_in_samples() -> None:
    c = linein.judge(heard(TONE_021))
    assert (c.note, c.problems) == ("10 tone s", [])
    # the old checker's false dropout: seconds told apart by number, not stamp
    c = linein.judge(heard(TONE_SOAK6))
    assert (c.note, c.problems) == ("10 tone s", [])
    # a whole second with a gap is one
    rows = list(TONE_021)
    stamp, ldb, lhz, lpct, rdb, rhz, rpct, _ = rows[6]
    rows[6] = (stamp, ldb, lhz, lpct, rdb, rhz, rpct, 20)
    assert linein.judge(heard(rows)).problems == [
        f"dropout {heard(rows)[6].low_line()}"]
    # the next clip (mono, at the purity threshold) is no part of a stereo clip's tone
    span = soak.tone_span(entry("codecs/19_adx.adx", "stereo"))
    c = linein.judge(heard(CODEC_033), span=span)
    assert (span, c.note, c.problems) == (9, "10 tone s", [])


def test_tone_span() -> None:
    assert soak.tone_span(entry("codecs/01_mp3_192.mp3", "stereo")) == 9
    assert soak.tone_span(entry("codecs/41_wv_24_96.wv", "tone", None)) == 11
    assert soak.tone_span(media.Entry("codecs/x.vox", "tone", None, None)) is None


def test_track_lines() -> None:
    want = "/ts17test/codecs/25_aiff_16_44.aiff"
    log = normal_log(RunRequest(Path("x.elf"), 50))
    assert soak.track_problems(log, want) == []                 # an older build: no lines
    played = f"{log}track: {want}\ntrack: /ts17test/codecs/26_alac_16_44.m4a\n"
    assert soak.track_problems(played, want) == []
    other = f"{log}track: /ts17test/codecs/26_alac_16_44.m4a\ntrack: {want}\n"
    assert soak.track_problems(other, want) == ["skipped 25_aiff_16_44.aiff"]
    never = f"{log}track: /ts17test/tone.mp3\n"
    assert soak.track_problems(never, want) == ["skipped 25_aiff_16_44.aiff"]


def test_video_stats() -> None:
    assert soak.video_stats(VIDEO_LINES) == ([], "shown 499 drop 0")
    bad = "video: pics end=20 shown=400 drop=99 late=0/6\nvideo: sound end=20 under=3/0 low=1\n"
    assert soak.video_stats(bad) == (["video dropped 99 of 499", "video sound underruns 3"],
                                     "shown 400 drop 99")
    assert soak.video_stats(BODY) == (["no video stats"], "")


def test_alarm_keys() -> None:
    log = normal_log(RunRequest(Path("x.elf"), 287))
    assert soak.saved_alarm(log) == (9, 15)
    assert soak.saved_alarm(WAKE_LOG) == (8, 51)
    assert soak.saved_alarm(BODY.replace("alarm 9:15", "")) is None
    # soak7 step 18: started 10:05:13, returned after 287 s
    now = datetime(2026, 11, 5, 10, 5, 13)
    plan = soak.alarm_keys((9, 15), now, 150)
    assert (plan.keys, plan.note) == (ALARM_SCRIPT, "set 10:09 from 09:15")
    assert int((plan.target - now).total_seconds()) + 60 == 287
    # soak6-po step 1: power-off, the alarm 08:51 woke it; return after 299 s
    now = datetime(2026, 11, 5, 8, 48, 1)
    plan = soak.alarm_keys((8, 45), now, 90)
    assert plan.note == "set 08:51 from 08:45" and plan.keys.endswith("1500 R U U U U U U S 3000")
    assert int((plan.target - now).total_seconds()) + 120 == 299
    # no alarm saved yet (-1:0): moved from the time the screen opens
    assert soak.alarm_keys((-1, 0), now, 90).note == "set 08:51 from 08:48"


def test_keys_fit_and_parse() -> None:
    t = Ts17(Workspace(Path("/nonexistent")))
    rng = random.Random(7)
    for _ in range(200):
        keys = soak.keys_random(rng, rng.randint(20, 120), "UDDSLDDS", (200, 1500),
                                head="9000 S 1500", limit=t.keys_max)
        assert keys.startswith("9000 S 1500 ") and len(keys) <= t.keys_max
        t.check_keys(keys)
    t.check_keys(soak.ALARM_MENU)
    assert ALARM_SCRIPT.startswith(f"{soak.ALARM_MENU} U R ")


def test_boot_judges_soak7_logs() -> None:
    def judged(ring: Callable[[RunRequest], str], **kw: Any) -> Outcome:
        with tempfile.TemporaryDirectory() as d:
            t = FakeTs17(Workspace(Path(d)), ring)
            return boot(step(t, "boot"), 25, **kw).outcome

    out = judged(normal_log, expect=("dac: powered up", "rtc: "))
    assert (out.problems, out.lost) == ([], False)
    assert judged(lambda r: normal_log(r).replace("returning", "leaving")).problems == [
        "early return (hang, reset or power-off)"]
    assert judged(lambda r: normal_log(r, "lcd: frame timed out")).problems == [
        "lcd: frame timed out"]
    refused = "pad GPIO26 REFUSED: owner 5, asked by 1"
    assert judged(lambda r: normal_log(r, refused)).problems == [refused]
    probe = "pad GPIO35 taken by a dev probe: owner 1, asked by 7"
    assert judged(lambda r: normal_log(r, probe)).problems == []
    assert judged(lambda r: normal_log(r).replace("dac: powered up", "dac: off"),
                  expect=("dac: powered up",)).problems == ["missing: dac: powered up"]


def test_codec_steps() -> None:
    with tempfile.TemporaryDirectory() as d:
        t = FakeTs17(workspace(Path(d), CARD_SET))
        line_in = LineIn(CODEC_033)
        with mock.patch.object(linein, "tone_check", line_in):
            # soak7 step 33: the script it pressed, its note
            out = soak.codec(step(t, "codec", Pick(18)))
            assert t.requests[-1].keys == "9000 S 1500 D D D D D S 1500 S 1500 " + \
                "D " * 18 + "S"
            assert (out.note, out.problems) == ("19_adx.adx 10 tone s", [])
            assert line_in.calls == [{"need": 6, "stereo": True, "span": 9}]
            # soak7 step 3: a music clip is judged by its log alone (soak6 step
            # 27 misjudged it as a tone clip: "tone seen in 5 s only")
            out = soak.codec(step(t, "codec", Pick(39)))
            assert t.requests[-1].keys.endswith("1500 S 1500 " + "D " * 39 + "S")
            assert (out.note, out.problems, len(line_in.calls)) == (
                "40_wv_16_44.wv music", [], 1)
        # a build that logs tracks, with the clip skipped
        t.ring = lambda r: normal_log(r, "track: /ts17test/codecs/41_wv_24_96.wv")
        out = soak.codec(step(t, "codec", Pick(39)))
        assert out.problems == ["skipped 40_wv_16_44.wv"]


def test_video_steps() -> None:
    with tempfile.TemporaryDirectory() as d:
        t = FakeTs17(workspace(Path(d), CARD_SET))
        line_in = LineIn(TONE_021)
        with mock.patch.object(linein, "tone_check", line_in):
            out = soak.video(step(t, "video", Pick(3)))     # soak7 step 7
            assert t.requests[-1].keys.startswith(
                "9000 S 1500 D D D D D S 1500 D S 1500 D D D S 8000 S ")
            assert (out.note, out.problems) == ("04_mjpeg.avi shown 499 drop 0 10 tone s", [])
            assert line_in.calls == [{"need": 5, "stereo": True, "span": 6}]
            out = soak.video(step(t, "video", Pick(1)))     # soak7 step 4
            assert t.requests[-1].keys.startswith(
                "9000 S 1500 D D D D D S 1500 D S 1500 D S 3000 S 8000 S ")
            assert (out.note, out.problems) == ("02_mpeg2.mpg 10 tone s", [])
            out = soak.video(step(t, "video", Pick(5)))     # soak7 step 14
            assert (out.note, out.problems, len(line_in.calls)) == (
                "06_land_ima.avi shown 499 drop 0", [], 2)


def test_recordings() -> None:
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        assert soak.recordings(root, datetime(2026, 11, 5, 9, 52)) == ("", [])
        recs = root / "Recordings"
        recs.mkdir()
        for name, size in (("R_MIC_261105-095000.wav", 44), ("R_MIC_261105-095555.wav", 1000),
                           ("R_MIC_261105-100000.wav", 44), ("R_FM_261105-100500.wav", 44)):
            (recs / name).write_bytes(bytes(size))
        assert soak.recordings(root, datetime(2026, 11, 5, 9, 52)) == (
            "2 new recordings", ["empty recordings: R_MIC_261105-100000.wav"])


def test_simulated_run() -> None:
    """Steps of every scenario but `usb` (it needs the card on this PC)
    through the engine: key scripts the player takes, logs judged, the run
    directory written"""
    n = 14
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        t = FakeTs17(workspace(root, CARD_SET))
        s = engine.RunSettings(root / "run", datetime(2100, 1, 1), seed=1793897233,
                               only=[x for x in WEIGHTS if x != "usb"])
        lines: list[str] = []

        def echo(line: str) -> None:
            lines.append(line)
            if len(lines) > n:                  # the header, then a line per step
                s.deadline = datetime.now()

        with mock.patch.object(linein, "tone_check", LineIn(TONE_021)):
            steps = engine.run(SoakSuite(t, root / "rockbox.elf", shots=False,
                                         settings={"volume": "-25"}), s, echo)
        assert len(steps) == n, lines
        assert all(x.ok for x in steps), "\n".join(lines)
        assert lines[-1] == f"done: {n} steps, 0 failed"
        logged = [json.loads(x) for x in (s.out / "steps.jsonl").read_text().splitlines()]
        assert [x["name"] for x in logged] == [x.name for x in steps]
        for x in steps:
            assert (s.out / f"{x.step:03d}-{x.name}.txt").read_text()
        alarms = sum(x.name in ("alarm", "poweroff") for x in steps)
        assert len(t.requests) == n + alarms     # an alarm step boots once more to read it
        assert any(r.cold for r in t.requests) == any(x.name == "boot" for x in steps)
        # every boot's, and the target's soak default
        assert all(r.settings == {"volume": "-25", "bluetooth": "off"} for r in t.requests)
