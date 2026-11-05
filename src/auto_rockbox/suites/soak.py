"""The soak suite: every step boots the image afresh with a key script and
judges the log it leaves, so it needs no dev shell and covers what a shell
cannot: cold boots, real key paths, alarms, power-off and USB attach.

Generic scenarios need nothing of the player's menus; a player's own
scenarios (targets/<name>/soak.py) know its menu layout and card.

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

from .. import engine
from ..engine import Outcome, StepContext, Suite
from ..registry import Context, arg, command
from ..run import run
from ..target import RunRequest, Target

# Rockbox's own log lines that mean a failure, whatever the player
FAULTS = (r"PANIC|\bfault\b|stkov|Undefined|\babort|keys: bad token|"
          r"codec load \d+ failed|codec start failed|codec header error|"
          r"no file, or over|returned error|not loaded|"
          r"arena (request refused|not acquired)|leaks file handles")


class SoakSuite(Suite):
    name = "soak"

    def __init__(self, target: Target, image: Path, shots: bool) -> None:
        super().__init__(target)
        self.image = image
        self.shots = shots
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
         expect: tuple[str, ...] = (), cold: bool = False, usb: bool = False) -> Boot:
    """Run the image for SECONDS with KEYS and judge its log: lost, an early
    return (no `return_line`), failure lines, lines EXPECT names missing"""
    s = suite(step)
    t = step.target
    req = RunRequest(s.image, seconds, keys, settings or {}, cold, usb)
    shots = step.out / "shots" / f"{step.index:03d}" if s.shots else None
    t0 = datetime.now(UTC)
    r = run(t, req, shots)
    t1 = datetime.now(UTC)
    probs: list[str] = []
    if r.returned and t.return_line and t.return_line not in r.log:
        probs.append("early return (hang, reset or power-off)")
    probs += [ln.strip() for ln in r.log.splitlines()
              if s.faults.search(ln) and not ln.startswith(("keys: done", "track: "))]
    probs += [f"missing: {e}" for e in expect if e not in r.log]
    out = Outcome(probs, output=r.log, lost=not r.returned, sheet=r.sheet)
    return Boot(out, t0, t1, r.log)


@command("soak", help="boot after boot with random scenarios until a deadline",
         needs={"framebuffer"}, args=[
             arg("image", type=Path, help="the dev build's rockbox.elf"),
             arg("out", type=Path, help="run directory: steps.jsonl, NNN-name.txt|png"),
             arg("--hours", type=float), arg("--until", metavar="HH:MM"),
             arg("--only", help="comma-separated scenarios"),
             arg("--weights", default="", help="name=weight,... overriding the defaults"),
             arg("--seed", type=int),
             arg("--shots", action="store_true",
                 help="a contact sheet (framebuffer + webcam) per step"),
         ])
def soak_cmd(ctx: Context, args: Namespace) -> int:
    s = SoakSuite(ctx.t, args.image, args.shots)
    seed = args.seed if args.seed is not None else int(datetime.now().timestamp())
    weights = {k: int(v) for k, _, v in (w.partition("=") for w in args.weights.split(",") if w)}
    steps = engine.run(s, engine.RunSettings(
        args.out, engine.deadline(args.hours, args.until), seed,
        args.only.split(",") if args.only else None, weights),
        echo=lambda line: print(line, flush=True))
    if not steps:
        print("no steps ran", file=sys.stderr)
    return 0 if steps and all(x.ok for x in steps) else 1
