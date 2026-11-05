"""The TS17's soak scenarios: whole boots with key scripts through its menus
and the card's test folder, judged from the log (suites/soak.py) and, where
a step plays the test tone, from Line In (linein.tone_check). No scenario
presses on/off or opens settings at random, so the player cannot power
itself off; only `poweroff` does, with the alarm set to wake it.

The card holds a media set (`arb media OUT`) in one folder: OUT's codecs/
and video/, and the tone as the folder's first file (folders sort first).
OUT/manifest.json says which clips carry the tone and where it steps down;
the scenarios open a file by its row in the browser, so the card must hold
the set the manifest describes.

    [targets.ts17.soak]
    media = "build/ts17-media"  # OUT (default <paths.build>/ts17-media)
    folder = "/ts17test"        # the card folder that holds it
    row = 5                     # that folder's row in the card root's file list (0: top)
    tone = "tone.mp3"           # the tone in that folder (1 kHz left, 1.5 kHz right)

Dev builds that log `track: PATH` as each track starts have a playback
step's first track checked: another file first means its own was skipped.
Builds without those lines are not checked.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from random import Random

from ... import card, linein, media
from ...engine import Outcome, StepContext, scenario
from ...errors import ArbError
from ...keys import TAP_MS, duration_ms
from ...suites.soak import Boot, boot
from ...workspace import ConfigError, Workspace

TS17 = {"ts17"}

BOOT_MS = 9000          # the image's start to its main menu: every script waits this first
OPEN_MS = 1500          # a menu or folder opening
# Main menu rows, and the test folder's (its folders, then the tone)
FILES, SETTINGS, RECORDING, RADIO = 0, 3, 4, 5
CODECS, VIDEO, TONE = 0, 1, 2
TONE_KINDS = ("stereo", "tone")     # media.Entry kinds that carry the test tone

PLAY_MS = 8000          # a video plays this long before the first pause
VIDEO_SPAN = 6          # tone seconds judged in a video: those before that pause
DROP_LIMIT = 0.05       # share of a video's pictures it may drop
PICS = re.compile(r"video: pics \S+ shown=(\d+) drop=(\d+)")
SOUND = re.compile(r"video: sound \S+ under=(\d+)/")
TRACK = "track: "       # a dev build's line as a track starts, with its path

LEARN_S = 20            # a boot that only reads the saved alarm from the log
LAUNCH_S = 9            # the host's part of a launch before the script starts, about
ALARM_TAPS = 45         # the alarm fields' presses at most: 12 hour, R, 30 minute, S and slack
SAVED_ALARM = re.compile(r"rtc: state 1;.*alarm (-?\d+):(\d+)")

USB_S = 90
CARD_WAIT_S = 60
SCRATCH = "soak.bin"
REC_PREFIX = "R_MIC_"   # recordings are named by the player's clock: R_MIC_yymmdd-HHMMSS.wav
WAV_HEADER = 44         # a recording this size holds no samples


def _keys(*parts: str | int) -> str:
    return " ".join(str(p) for p in parts if p != "")


def _pick(row: int) -> str:
    """Down to ROW of a list (0: the top) and select it"""
    return _keys(*["D"] * row, "S")


# Settings, its row 6, that row 1: the wake-up alarm, at its hour field
ALARM_MENU = _keys(BOOT_MS, _pick(SETTINGS), OPEN_MS, _pick(6), 2000, _pick(1), OPEN_MS)


def keys_random(rng: Random, n: int, letters: str, gap: tuple[int, int], *, head: str,
                limit: int) -> str:
    """HEAD and N random presses of LETTERS with GAP ms after each, fewer if
    the script would pass LIMIT bytes"""
    keys = head
    for _ in range(n):
        more = _keys(keys, rng.choice(letters), rng.randint(*gap))
        if len(more.encode()) > limit:
            break
        keys = more
    return keys


# ---------------------------------------------------------------- the card

@dataclass(frozen=True)
class Bench:
    """The media set on this PC, and where the card holds it"""

    files: tuple[media.Entry, ...]
    folder: str             # on the card, e.g. "/ts17test"
    row: int
    tone: str

    @classmethod
    def load(cls, ws: Workspace) -> Bench:
        conf = ws.section("targets", "ts17", "soak")
        directory = conf.opt_path("media") or ws.build_dir / "ts17-media"
        return cls(tuple(media.load(directory)), "/" + conf.string("folder").strip("/"),
                   conf.integer("row"), conf.string("tone"))

    def clips(self, sub: str) -> list[media.Entry]:
        """SUB's files in the browser's order"""
        out = sorted((e for e in self.files if e.path.startswith(f"{sub}/")),
                     key=lambda e: e.path)
        if not out:
            raise ConfigError(f"the media set has no {sub}/ files")
        return out

    def card_path(self, name: str) -> str:
        return f"{self.folder}/{name}"

    def open_folder(self) -> str:
        """Keys from the image's start into the test folder"""
        return _keys(BOOT_MS, _pick(FILES), OPEN_MS, _pick(self.row), OPEN_MS)


def tone_span(e: media.Entry) -> int | None:
    """How many of clip E's tone seconds to judge for dropouts: those before
    its step-down (or its end, after which the next file plays), less the one
    before the step, which can carry the step's edge"""
    end = e.step_down if e.step_down is not None else e.duration
    return None if end is None else max(int(end) - 1, 1)


# ---------------------------------------------------------------- judging

def track_problems(log: str, expected: str) -> list[str]:
    """The first `track:` line must name EXPECTED (a card path); another file
    first means EXPECTED was skipped. A log without them (older builds) passes."""
    tracks = [ln.removeprefix(TRACK).strip() for ln in log.splitlines() if ln.startswith(TRACK)]
    if not tracks or tracks[0] == expected:
        return []
    return [f"skipped {PurePosixPath(expected).name}"]


def video_stats(log: str) -> tuple[list[str], str]:
    """The Video Player's dev stats (its last `video:` lines): too many
    pictures dropped, sound underruns. Problems and a note."""
    pics, sound = PICS.findall(log), SOUND.findall(log)
    probs: list[str] = []
    note = ""
    if not pics:
        probs.append("no video stats")
    else:
        shown, drop = (int(x) for x in pics[-1])
        note = f"shown {shown} drop {drop}"
        if drop > DROP_LIMIT * max(shown + drop, 1):
            probs.append(f"video dropped {drop} of {shown + drop}")
    if sound and int(sound[-1]):
        probs.append(f"video sound underruns {sound[-1]}")
    return probs, note


def _heard(step: StepContext, b: Boot, label: str, *, need: int = 6, stereo: bool = True,
           span: int | None = None) -> Outcome:
    """B's outcome with Line In's verdict on the tone after the boot"""
    c = linein.tone_check(linein.directory(step.target.ws),
                          b.t0 + timedelta(milliseconds=BOOT_MS), b.t1,
                          need=need, stereo=stereo, span=span)
    out = b.outcome
    out.problems += c.problems
    out.note = _keys(label, c.note)
    out.output = f"{b.log}\n{c.report()}\n"
    return out


# ---------------------------------------------------------------- playback

@scenario("tone", suite="soak", weight=2, target=TS17)
def tone(step: StepContext) -> Outcome:
    """The tone file from the browser plays whole on Line In"""
    bench = Bench.load(step.target.ws)
    b = boot(step, 40, _keys(bench.open_folder(), _pick(TONE)))
    if b.outcome.lost:
        return b.outcome
    b.outcome.problems += track_problems(b.log, bench.card_path(bench.tone))
    return _heard(step, b, "")


@scenario("codec", suite="soak", weight=5, target=TS17)
def codec(step: StepContext) -> Outcome:
    """A random clip of codecs/: a tone clip plays whole on Line In (the
    folder plays on into the next clip, so only up to its step-down); a
    music clip is judged by the log alone"""
    bench = Bench.load(step.target.ws)
    clips = bench.clips("codecs")
    i = step.rng.randrange(len(clips))
    e = clips[i]
    name = PurePosixPath(e.path).name
    b = boot(step, 50, _keys(bench.open_folder(), _pick(CODECS), OPEN_MS, _pick(i)))
    b.outcome.note = name
    if b.outcome.lost:
        return b.outcome
    b.outcome.problems += track_problems(b.log, bench.card_path(e.path))
    if e.kind not in TONE_KINDS:
        b.outcome.note = f"{name} {e.kind}"
        return b.outcome
    return _heard(step, b, name, stereo=e.kind == "stereo", span=tone_span(e))


@scenario("video", suite="soak", weight=3, target=TS17)
def video(step: StepContext) -> Outcome:
    """A random video of video/, paused and resumed 1-3 times: the Video
    Player's stats (mpegplayer has none), and the tone on Line In before the
    first pause if the video carries it. Only Select: the arrows change the
    volume (as the picture is turned), which would carry over to later steps."""
    bench = Bench.load(step.target.ws)
    clips = bench.clips("video")
    i = step.rng.randrange(len(clips))
    e = clips[i]
    name = PurePosixPath(e.path).name
    mpg = name.endswith(".mpg")
    parts: list[str | int] = [bench.open_folder(), _pick(VIDEO), OPEN_MS, _pick(i)]
    if mpg:
        parts += [3000, "S"]                # mpegplayer's start menu: its first entry plays
    parts.append(PLAY_MS)
    for _ in range(step.rng.randint(1, 3)):
        parts += ["S", step.rng.randint(500, 3000), "S", step.rng.randint(2000, 6000)]
    b = boot(step, 60, _keys(*parts))
    b.outcome.note = name
    if b.outcome.lost:
        return b.outcome
    if not mpg:
        probs, stats = video_stats(b.log)
        b.outcome.problems += probs
        b.outcome.note = _keys(name, stats)
    if e.kind not in TONE_KINDS:
        return b.outcome
    return _heard(step, b, b.outcome.note, need=5, stereo=e.kind == "stereo", span=VIDEO_SPAN)


@scenario("wps", suite="soak", weight=2, target=TS17)
def wps(step: StepContext) -> Outcome:
    """The tone file, then random Left, Right and Select on the playback
    screen; no Up or Down: the volume is saved and would carry over"""
    bench = Bench.load(step.target.ws)
    head = _keys(bench.open_folder(), _pick(TONE), 3000)
    keys = keys_random(step.rng, step.rng.randint(15, 30), "RLSRLRL", (300, 2500), head=head,
                       limit=step.target.keys_max)
    b = boot(step, 75, keys)
    if not b.outcome.lost:
        b.outcome.problems += track_problems(b.log, bench.card_path(bench.tone))
    return b.outcome


# ---------------------------------------------------------------- other screens

@scenario("browse", suite="soak", weight=2, target=TS17)
def browse(step: StepContext) -> Outcome:
    """Random walks through the file browser"""
    head = _keys(BOOT_MS, _pick(FILES), OPEN_MS)
    keys = keys_random(step.rng, step.rng.randint(20, 40), "UDDSLDDS", (200, 1500), head=head,
                       limit=step.target.keys_max)
    return boot(step, 70, keys).outcome


@scenario("fm", suite="soak", weight=1, target=TS17)
def fm(step: StepContext) -> Outcome:
    """The radio screen: random tuning and presets"""
    head = _keys(BOOT_MS, _pick(RADIO), 6000)
    keys = keys_random(step.rng, step.rng.randint(4, 10), "RLS", (1500, 5000), head=head,
                       limit=step.target.keys_max)
    return boot(step, 60, keys).outcome


@scenario("rec", suite="soak", weight=2, target=TS17)
def rec(step: StepContext) -> Outcome:
    """A recording: Select starts, pauses and resumes it, a held Left stops
    it (closing the file), a second leaves the screen. `usb` checks the
    files hold samples."""
    keys = _keys(BOOT_MS, _pick(RECORDING), 4000, "S", step.rng.randint(5000, 20000),
                 "S", 2000, "S", step.rng.randint(3000, 10000), "L+", 3000, "L+", 2000)
    return boot(step, 60, keys).outcome


@scenario("boot", suite="soak", weight=2, target=TS17, needs={"cold"})
def cold_boot(step: StepContext) -> Outcome:
    """A cold boot through the boot chain, no keys: the DAC and clock come up"""
    return boot(step, 25, cold=True, expect=("dac: powered up", "rtc: ")).outcome


# ---------------------------------------------------------------- USB

def recordings(root: Path, since: datetime) -> tuple[str, list[str]]:
    """The recordings on the card at ROOT made since SINCE (the player's
    clock names them): a note, and a problem naming any without samples"""
    first = since.strftime(f"{REC_PREFIX}%y%m%d-%H%M%S")
    d = root / "Recordings"
    new = [p for p in d.iterdir() if p.name.startswith(REC_PREFIX) and p.name >= first] \
        if d.is_dir() else []
    if not new:
        return "", []
    empty = sorted(p.name for p in new if p.stat().st_size <= WAV_HEADER)
    return f"{len(new)} new recordings", ([f"empty recordings: {' '.join(empty)}"]
                                          if empty else [])


def _round_trip(uuid: str | None, size: int, since: datetime, started: float,
                alive: Callable[[], bool]) -> tuple[list[str], str]:
    """SIZE random bytes onto the card and back: problems and a note"""
    try:
        dev = card.wait(uuid, CARD_WAIT_S, alive)
    except ArbError as e:
        return [str(e)], ""
    data = os.urandom(size)
    probs: list[str] = []
    rec_note = ""
    try:
        with card.mounted(dev) as mp:
            (mp / SCRATCH).write_bytes(data)
        with card.mounted(dev) as mp:       # a fresh mount: the card is read, not the page cache
            f = mp / SCRATCH
            if f.read_bytes() != data:
                probs.append(f"USB read-back differs ({size} bytes)")
            f.unlink()
            rec_note, rec_probs = recordings(mp, since)
            probs += rec_probs
    except (OSError, ArbError) as e:
        return [f"USB step: {e}"], ""
    note = f"{size >> 20} MiB in {time.monotonic() - started:.0f} s"
    return probs, f"{note}; {rec_note}" if rec_note else note


@scenario("usb", suite="soak", weight=2, target=TS17, needs={"usb", "storage"})
def usb(step: StepContext) -> Outcome:
    """A boot with Rockbox's USB on: a random 2-12 MiB file written to the
    card and read back, and the recordings this run made hold samples"""
    uuid = step.target.conf.string("card_uuid", "") or None
    size = step.rng.randint(2, 12) << 20
    started = time.monotonic()
    with ThreadPoolExecutor(1) as pool:
        running = pool.submit(boot, step, USB_S, usb=True)
        probs, note = _round_trip(uuid, size, step.run_start, started,
                                  lambda: not running.done())
        b = running.result()
    b.outcome.problems[:0] = probs
    b.outcome.note = note
    return b.outcome


# ---------------------------------------------------------------- the alarm

def saved_alarm(log: str) -> tuple[int, int] | None:
    """The wake-up alarm the player has saved, from a boot log's clock line"""
    m = SAVED_ALARM.search(log)
    return (int(m[1]), int(m[2])) if m else None


def _field(start: int, to: int, size: int) -> str:
    """Presses that move a field of SIZE values from START to TO"""
    d = (to - start) % size
    return _keys(*["U"] * d) if d <= size // 2 else _keys(*["D"] * (size - d))


@dataclass(frozen=True)
class AlarmPlan:
    keys: str
    target: datetime        # the player's clock (this PC's local time)
    note: str


def alarm_keys(saved: tuple[int, int], now: datetime, lead_s: float) -> AlarmPlan:
    """Keys that set the alarm to the first whole minute LEAD_S after its
    screen's keys are done, moved from SAVED (or the time the screen opens,
    if SAVED is no time of day)"""
    opened = now + timedelta(seconds=LAUNCH_S, milliseconds=duration_ms(ALARM_MENU))
    h0, m0 = saved
    if not (0 <= h0 < 24 and 0 <= m0 < 60):
        h0, m0 = opened.hour, opened.minute
    at = opened + timedelta(milliseconds=ALARM_TAPS * TAP_MS, seconds=lead_s)
    at = (at + timedelta(minutes=1)).replace(second=0, microsecond=0)
    keys = _keys(ALARM_MENU, _field(h0, at.hour, 24), "R", _field(m0, at.minute, 60), "S", 3000)
    return AlarmPlan(keys, at, f"set {at:%H:%M} from {h0:02d}:{m0:02d}")


def _alarm(step: StepContext, lead_s: float, after_s: int, press: str,
           expect: tuple[str, ...]) -> Outcome:
    """A boot that reads the saved alarm, then one that sets it LEAD_S ahead,
    presses PRESS and runs until AFTER_S past the alarm, expecting EXPECT"""
    learn = boot(step, LEARN_S)
    saved = saved_alarm(learn.log)
    if learn.outcome.lost or saved is None:
        if not learn.outcome.lost:
            learn.outcome.problems.append("saved alarm not in the boot log")
        return learn.outcome
    now = datetime.now()
    plan = alarm_keys(saved, now, lead_s)
    seconds = int((plan.target - now).total_seconds()) + after_s
    b = boot(step, seconds, _keys(plan.keys, press), expect=expect)
    b.outcome.problems[:0] = learn.outcome.problems
    b.outcome.note = plan.note
    b.outcome.output = f"{learn.log}\n{b.log}"
    return b.outcome


@scenario("alarm", suite="soak", weight=1, target=TS17)
def alarm(step: StepContext) -> Outcome:
    """The wake-up alarm, set about 3 minutes ahead, fires while on"""
    return _alarm(step, 150, 60, "", ("rtc: alarm in", "rtc: alarm while on"))


@scenario("poweroff", suite="soak", weight=1, target=TS17)
def poweroff(step: StepContext) -> Outcome:
    """Power-off by the on/off key with the alarm about 2 minutes ahead: the
    alarm starts the player again and the clock comes back from the PMU
    counter. The log is the wake boot's, which returns after the dev build's
    own time. A wake that fails leaves the player off (lost) until someone
    presses on/off."""
    return _alarm(step, 90, 120, "P+++", ("alarm woke 1", "rtc: restored"))
