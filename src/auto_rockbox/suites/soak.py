"""The soak suite: every step boots the image afresh with a key script and
judges the log it leaves, so it needs no dev shell and covers what a shell
cannot: cold boots, real key paths, alarms, power-off and USB attach.

Generic scenarios (here) need nothing of the player's menus: they use the
run settings every dev build takes (apps/dev_shell.c dev_boot: sdtest,
crash, poweroff, volume, play, sh, bluetooth) and the Target API, so a
player runs them as soon as its dev loop works, long before its dev shell
does. A player's own scenarios (targets/<name>/soak.py) know its menu
layout and card; one of the same name replaces the generic one.

    arb ts17 soak build/ts17/rockbox.elf build/soak --until 20:00 --shots
"""

from __future__ import annotations

import hashlib
import re
import sys
import time
from argparse import Namespace
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .. import camera, card, engine, fmcheck, linein
from ..engine import Outcome, StepContext, Suite, scenario
from ..errors import ArbError
from ..registry import Context, arg, command
from ..run import parse_settings, run
from ..target import RunRequest, Target

# Rockbox's own log lines that mean a failure, whatever the player
FAULTS = (r"PANIC|\bfault\b|stkov|Undefined|\babort|keys: bad token|"
          r"codec load \d+ failed|codec start failed|codec header error|"
          r"no file, or over|returned error|not loaded|"
          r"arena (request refused|not acquired)|leaks file handles")

BOOT_S = 30             # a step's run time when it only needs the boot
WIRED = {"bluetooth": "off"}    # a player that pairs at boot sends its audio elsewhere
TONE_DB = "-20"         # the tone step's volume (dB): the level the torture's tone plays at
CRASH = "crash test"    # dev_boot's lines for crash=panic: "crash test: panic", the panic's
CRASH_PANIC = "dev crash test 42"
POWEROFF = ("poweroff test: posting SYS_POWEROFF", "power_off() (dry run): back to the host")
SDTEST = re.compile(r"sdtest \d+ KiB.* errors (-?\d+)")
STORAGE_S = 120         # the card's time on this PC for its check
FLICK_ROWS = 60         # the flick step's list: empty folders in /flick
FLICK_FPS = 30          # a coast's floor (a refresh is 45 to 60 a second)
FLING = re.compile(r"^fling (-?\d+) px/s( \(boosted\))?", re.M)
COAST = re.compile(r"^coast (-?\d+) px/s: (-?\d+) px in (\d+) ms, (\d+) frames(, at the end)?",
                   re.M)
CAUGHT = re.compile(r"^coast -?\d+ px/s: caught at", re.M)
FMSTAT = re.compile(r"^fmstat (\d+) kHz: present (\d) tuned (\d) stereo (\d) "
                    r"rssi (\d+( \(backlight off\))?); rds pi ([0-9a-f]{4}) '(.*)'$", re.M)
FM_FLOOR_DBFS = -35.0   # quieter Line In seconds are not the radio (~-16 dBFS RMS at 0 dB)
ROCKS = "/.rockbox/rocks"
USB_HASH_MIB = 16       # the usb-hash step's file
USB_HASH_FILE = "arb-usbhash.bin"
GAME_S = 45             # a game step's run time: the boot, the script and a margin


class SoakSuite(Suite):
    name = "soak"

    def __init__(self, target: Target, image: Path, shots: bool,
                 settings: dict[str, str] | None = None) -> None:
        super().__init__(target)
        self.image = image
        self.shots = shots
        # every boot's, under the step's own
        self.settings = {**target.soak_settings, **(settings or {})}
        extra = target.log_faults
        self.faults = re.compile(FAULTS + (f"|{extra}" if extra else ""), re.I)

    def start(self, out: Path) -> None:
        if self.shots:      # every step would fail on it at once
            camera.check_fresh(self.target.ws)


def suite(step: StepContext) -> SoakSuite:
    return cast(SoakSuite, step.suite)


@dataclass
class Boot:
    """One boot of a soak step: its outcome and when it ran (UTC, for Line In)"""

    outcome: Outcome
    t0: datetime
    t1: datetime
    log: str = ""


def boot(step: StepContext, seconds: int, keys: str = "", *, settings: dict[str, str] | None = None,
         expect: tuple[str, ...] = (), allow: tuple[str, ...] = (), ends_early: bool = False,
         cold: bool = False) -> Boot:
    """Run the image for SECONDS with KEYS and judge its log: lost, an early
    return (no `return_line`) unless the step ENDS_EARLY on purpose, failure
    lines but those that contain one of ALLOW, lines EXPECT names missing"""
    s = suite(step)
    t = step.target
    req = RunRequest(s.image, seconds, keys, {**s.settings, **(settings or {})}, cold)
    shots = step.out / "shots" / f"{step.index:03d}" if s.shots else None
    t0 = datetime.now(UTC)
    r = run(t, req, shots)
    t1 = datetime.now(UTC)
    probs: list[str] = []
    if r.returned and t.return_line and t.return_line not in r.log and not ends_early:
        probs.append("early return (hang, reset or power-off)")
    probs += [ln.strip() for ln in r.log.splitlines()
              if s.faults.search(ln) and not ln.startswith(("keys: done", "track: "))
              and not any(a in ln for a in allow)]
    probs += [f"missing: {e}" for e in expect if e not in r.log]
    out = Outcome(probs, output=r.log, lost=not r.returned, sheet=r.sheet)
    return Boot(out, t0, t1, r.log)


# ---------------------------------------------------------------- every player's scenarios

@scenario("boot", suite="soak", area="boot", weight=2)
def plain_boot(step: StepContext) -> Outcome:
    """A boot and nothing else: it runs its time with a clean log and returns
    on time"""
    return boot(step, BOOT_S).outcome


@scenario("crash", suite="soak", area="boot", weight=1)
def crash(step: StepContext) -> Outcome:
    """A panic at boot (crash=panic): the log keeps it and the player comes
    back to the host by itself, as it must for runs to collect a crash"""
    return boot(step, BOOT_S, settings={"crash": "panic"}, expect=(CRASH_PANIC,),
                allow=(CRASH,), ends_early=True).outcome


@scenario("poweroff", suite="soak", area="power", weight=1, needs={"usb-poweroff"})
def poweroff(step: StepContext) -> Outcome:
    """A power-off as the menu's (poweroff=, dry): the shutdown runs to its
    end and the player returns to the host instead of switching off. Only
    a build that powers off on a charger can do it on the bench (Rockbox's
    default shows the charging splash instead)"""
    return boot(step, 60, settings={"poweroff": "1"}, expect=POWEROFF, ends_early=True).outcome


@scenario("sdtest", suite="soak", area="storage", weight=2)
def sdtest(step: StepContext) -> Outcome:
    """SD write, fsync and read-back at boot (sdtest=), half the time
    through the unaligned path: no errors"""
    bufkb = step.rng.choice([4, 16, 32])
    kb = bufkb * step.rng.randint(16, 128)
    odd = " odd" if step.rng.random() < 0.5 else ""
    out = boot(step, 90, settings={"sdtest": f"{kb} {bufkb}{odd}"}).outcome
    out.note = f"{kb} KiB {bufkb}{odd}"
    if not out.lost:
        m = SDTEST.search(out.output)
        if not m:
            out.problems.append("no sdtest result in the log")
        elif m[1] != "0":
            out.problems.append(m[0])
    return out


@scenario("fsck", suite="soak", area="storage", weight=1, needs={"storage"})
def fsck(step: StepContext) -> Outcome:
    """The card on this PC as USB mass storage: a clean filesystem check
    (whatever the steps before wrote)"""
    try:
        with step.target.mass_storage(STORAGE_S, suite(step).image) as dev:
            c = card.check(dev)
    except ArbError as e:
        return Outcome([f"no card check: {e}"])
    return Outcome([] if c.clean else [c.describe()], c.describe())


@scenario("usb-hash", suite="soak", area="usb", weight=1, needs={"storage"})
def usb_hash(step: StepContext) -> Outcome:
    """USB mass storage moves data intact: a random file written to the
    card in one session reads back with its SHA-256 in the next, the player
    booted again in between (no cache on either side serves the read)"""
    data = step.rng.randbytes(USB_HASH_MIB << 20)
    want = hashlib.sha256(data).hexdigest()
    t, image = step.target, suite(step).image
    try:
        with t.mass_storage(STORAGE_S, image) as dev:
            t0 = time.monotonic()
            with card.mounted(dev) as mp:
                (mp / USB_HASH_FILE).write_bytes(data)
            write_s = time.monotonic() - t0
        with t.mass_storage(STORAGE_S, image) as dev:
            t0 = time.monotonic()
            with card.mounted(dev) as mp:
                got = hashlib.sha256((mp / USB_HASH_FILE).read_bytes()).hexdigest()
                read_s = time.monotonic() - t0
                (mp / USB_HASH_FILE).unlink()
    except (ArbError, OSError) as e:
        return Outcome([f"mass storage: {e}"])
    note = (f"{USB_HASH_MIB} MiB written in {write_s:.1f} s, read in {read_s:.1f} s "
            "(with the mount)")
    return Outcome([] if got == want else [f"read back sha256 {got[:16]}, wrote {want[:16]}"],
                   note=note, output=f"{note}\nwrote {want}\nread  {got}\n")


@scenario("tone", suite="soak", area="audio", weight=2)
def tone(step: StepContext) -> Outcome:
    """The dev folder's tone.mp3 played at boot (play=, a fixed volume,
    Bluetooth off): both tones whole on Line In"""
    t = step.target
    directory = linein.directory(t.ws)
    if not t.dev_dir:
        return Outcome(note=f"skipped: {t.name} has no dev_dir", skipped=True)
    if not linein.running(directory):
        return Outcome(note="skipped: no Line In capture running (arb linein serve)",
                       skipped=True)
    b = boot(step, 40, settings={**WIRED, "volume": TONE_DB, "play": f"{t.dev_dir}/tone.mp3"})
    if b.outcome.lost:
        return b.outcome
    c = linein.tone_check(directory, b.t0, b.t1)
    out = b.outcome
    out.problems += c.problems
    out.note = c.note
    out.output = f"{b.log}\n{c.report()}\n"
    return out


def _flick_folder(step: StepContext) -> str | None:
    """/flick with its rows on the card, made where missing: a problem, or None"""
    try:
        with (step.target.mass_storage(STORAGE_S, suite(step).image) as dev,
              card.mounted(dev) as mp):
            for i in range(1, FLICK_ROWS + 1):
                (mp / "flick" / f"Row {i:02d}").mkdir(parents=True, exist_ok=True)
    except (ArbError, OSError) as e:
        return f"no /flick on the card: {e}"
    return None


@scenario("flick", suite="soak", area="ui", weight=1, needs={"touch", "storage"})
def flick(step: StepContext) -> Outcome:
    """Lists under the finger (HAVE_TOUCH_FLING's log): in a list of 60
    rows, half the time with the tone repeating (whole on Line In, when
    it runs), a flick down and one up each coast and end; a short flick
    scrolls instead of opening a row; taps on coasting lists catch them and
    open nothing; a flick right after a catch is boosted; coasts keep
    FLICK_FPS. Touches stay off the screen's edges and its bottom fifth (a
    mini player)"""
    if problem := _flick_folder(step):
        return Outcome([problem])
    t = step.target
    x, h = t.lcd.width // 2, t.lcd.height
    low, high, mid = h * 3 // 4, h * 22 // 100, h // 2
    up, down = f"@{x}:{low}>{x}:{high}~", f"@{x}:{high + 20}>{x}:{low + 10}~"
    keys = (f"12000 {up} 3000 {down} 3000 @{x}:{low}>{x}:{low - h * 3 // 32}~ 2000 "
            f"{up} @{x}:{mid} 2000 {up} {up} 4000")
    music = step.rng.random() < 0.5 and bool(t.dev_dir)
    settings = {"sh": "browse /flick/Row 01"}
    if music:
        # repeated: the tone's loud half (the part judged) under the flicks too
        settings = {**WIRED, "volume": TONE_DB, "play": f"{t.dev_dir}/tone.mp3",
                    "sh": settings["sh"] + ";repeat 2"}
    b = boot(step, 40, keys, settings=settings)
    out = b.outcome
    out.note = "with music" if music else "quiet"
    if out.lost:
        return out
    if music and linein.running(directory := linein.directory(t.ws)):
        c = linein.tone_check(directory, b.t0, b.t1)
        out.problems += c.problems
        out.note += f" ({c.note})"
    flings, coasts = FLING.findall(out.output), COAST.findall(out.output)
    if len(flings) != 6:
        out.problems.append(f"{len(flings)} flings of 6 (a flick taken for a tap?)")
    if not any(b for _, b in flings):
        out.problems.append("no boosted fling")
    if (caught := len(CAUGHT.findall(out.output))) != 2:
        out.problems.append(f"{caught} of 2 touches caught a coast (a tap, the last flick)")
    if "transition push" in out.output:
        out.problems.append("a row opened")
    rates = [(v, int(frames) * 1000 // int(ms)) for v, _, ms, frames, end in coasts
             if int(ms) >= 500 and not end]
    out.note += "; coasts at " + ", ".join(f"{fps}" for _, fps in rates) + " fps"
    out.problems += [f"coast {v} px/s at {fps} fps" for v, fps in rates if fps < FLICK_FPS]
    if len(coasts) < 3:
        out.problems.append(f"{len(coasts)} coasts ended of 3 or more")
    return out


@scenario("fm-station", suite="soak", area="fm", weight=1, needs={"fm"})
def fm_station(step: StepContext) -> Outcome:
    """The radio screen on the workspace's reference station ([fm] station
    in kHz, pi its RDS PI): tuned there with the station's PI decoded, which
    proves the tuner's reference clock (fm-radio.md); on Line In, when it
    runs, the monitor at 0 dB without an ADC overload wrap (fmcheck)"""
    conf = step.target.ws.section("fm")
    khz, pi = conf.integer("station", 0), conf.string("pi", "").lower()
    if not khz:
        return Outcome(note="skipped: no [fm] station in the workspace", skipped=True)
    # read before the backlight goes off, which leaves the RSSI unread
    b = boot(step, 40, "12000",
             settings={**WIRED, "volume": "0", "sh": f"radio {khz}", "then": "fmstat"})
    out = b.outcome
    if out.lost:
        return out
    if not (m := FMSTAT.search(out.output)):
        out.problems.append("no fmstat line")
        return out
    _, present, tuned, stereo, rssi, _, got, name = m.groups()
    out.note = f"rssi {rssi}, stereo {stereo}, pi {got} {name.strip()!r}"
    if present != "1" or tuned != "1":
        out.problems.append(f"not tuned to {khz} kHz (present {present}, tuned {tuned})")
    if pi and got != pi:
        out.problems.append(f"RDS PI {got} at {khz} kHz, not {pi}: the reference clock?")
    if linein.running(directory := linein.directory(step.target.ws)):
        x, rate = linein.recording(directory, b.t0, b.t1, floor_dbfs=FM_FLOOR_DBFS)
        if not rate:
            out.problems.append("no radio on Line In")
            return out
        c = fmcheck.check_frames(x, rate)
        out.note += f"; Line In {c.seconds:.0f} s, peak {c.peak_dbfs:.1f} dBFS"
        if c.wraps:
            out.problems.append(f"{c.wraps} overload wraps (at {c.wrap_times} s)")
    return out


@scenario("games", suite="soak", area="plugins", weight=1)
def games(step: StepContext) -> Outcome:
    """One of the player's games (Target.games, in turn by step), run from
    the shell at boot and played by its script, which quits it with the
    player's keys: the plugin returns PLUGIN_OK (a game that the keys
    cannot leave is a keymap bug) and the log stays clean"""
    t = step.target
    if not t.games:
        return Outcome(note=f"skipped: {t.name} lists no games", skipped=True)
    rock, script = sorted(t.games.items())[(step.index - 1) % len(t.games)]
    path = f"{ROCKS}/{rock}"
    b = boot(step, GAME_S, script, settings={"sh": f"run {path}"})
    out = b.outcome
    out.note = rock
    if out.lost:
        return out
    run = re.escape(f"run {path}")      # dev_shell.c ui_run_request's lines
    rc = re.search(rf"^{run}: rc (-?\d+)$", out.output, re.M)
    if not re.search(rf"^{run}$", out.output, re.M):
        out.problems.append("the game did not start")
    elif not rc:
        out.problems.append("did not quit with the player's keys (a keymap bug?)")
    elif rc.group(1) != "0":
        out.problems.append(f"returned {rc.group(1)}, not PLUGIN_OK")
    return out


# ---------------------------------------------------------------- the command

@command("soak", help="boot after boot with random scenarios until a deadline", args=[
             arg("image", type=Path, help="the dev build's rockbox.elf"),
             arg("out", type=Path, help="run directory: steps.jsonl, NNN-name.txt|png"),
             arg("--hours", type=float), arg("--until", metavar="HH:MM"),
             arg("--only", help="comma-separated scenarios"),
             arg("--weights", default="", help="name=weight,... overriding the defaults"),
             arg("--seed", type=int),
             arg("--shots", action="store_true",
                 help="a contact sheet (framebuffer + webcam) per step"),
             arg("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE",
                 help="a run setting for every boot, as `run --set` (e.g. a fixed volume, "
                 "so that no step's keys carry a volume change into the next)"),
         ])
def soak_cmd(ctx: Context, args: Namespace) -> int:
    s = SoakSuite(ctx.t, args.image, args.shots, parse_settings(args.settings))
    seed = args.seed if args.seed is not None else int(datetime.now().timestamp())
    weights = {k: int(v) for k, _, v in (w.partition("=") for w in args.weights.split(",") if w)}
    steps = engine.run(s, engine.RunSettings(
        args.out, engine.deadline(args.hours, args.until), seed,
        args.only.split(",") if args.only else None, weights),
        echo=lambda line: print(line, flush=True))
    if not steps:
        print("no steps ran", file=sys.stderr)
    return 0 if steps and all(x.ok for x in steps) else 1
