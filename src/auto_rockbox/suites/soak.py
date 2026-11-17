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

import re
import sys
from argparse import Namespace
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .. import card, engine, linein
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
            clean = card.check(dev)
    except ArbError as e:
        return Outcome([f"no card check: {e}"])
    return Outcome([] if clean else [card.describe_check(clean)],
                   card.describe_check(clean))


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
