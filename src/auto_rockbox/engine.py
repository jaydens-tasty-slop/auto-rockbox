"""Long randomised test runs: the loop that soak and torture share.

A run picks scenarios at random by weight until a deadline, writes what each
step saw, and stops early only when the player is lost and the suite cannot
get it back. A **suite** decides what a step is and what is checked between
steps:

    soak     every step is a whole boot (`Target.launch` with a key script):
             cold boots, real keys, alarms, USB. Needs no dev shell.
    torture  one boot, many steps over the dev shell; health counters after
             every step, crashes collected and the player booted again.

Scenarios register like commands (registry.py):

    @scenario("tone", suite="soak", weight=2)
    def tone(step: StepContext) -> Outcome: ...

    @scenario("fmregs", suite="torture", target={"yp3"}, needs={"fm"})

`needs` names target capabilities or suite features; a scenario is only in a
run whose target and suite have them all. Per step the run directory gets
`NNN-name.txt` (the scenario's output), a line in `steps.jsonl`, and
`NNN-name.png` when the suite makes contact sheets.
"""

from __future__ import annotations

import json
import random
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .target import Target


@dataclass
class Outcome:
    """What a step found. LOST: the player did not come back"""

    problems: list[str] = field(default_factory=list)
    note: str = ""
    output: str = ""
    lost: bool = False
    sheet: Path | None = None       # a contact sheet to keep as NNN-name.png


@dataclass
class StepContext:
    index: int
    name: str
    target: Target
    suite: Suite
    rng: random.Random
    out: Path                       # the run directory
    run_start: datetime

    def path(self, suffix: str) -> Path:
        """A file of this step in the run directory, e.g. path(".wav")"""
        return self.out / f"{self.index:03d}-{self.name}{suffix}"


ScenarioFn = Callable[[StepContext], Outcome]


@dataclass
class Scenario:
    name: str
    fn: ScenarioFn
    suite: str
    weight: int = 1
    targets: frozenset[str] | None = None
    needs: frozenset[str] = frozenset()


_scenarios: list[Scenario] = []


def scenario(name: str, *, suite: str, weight: int = 1,
             target: Iterable[str] | None = None,
             needs: Iterable[str] = ()) -> Callable[[ScenarioFn], ScenarioFn]:
    def register(fn: ScenarioFn) -> ScenarioFn:
        _scenarios.append(Scenario(name, fn, suite, weight,
                                   None if target is None else frozenset(target),
                                   frozenset(needs)))
        return fn
    return register


def scenarios_for(suite: Suite) -> dict[str, Scenario]:
    """The suite's scenarios for its target; a player's own beat generic ones"""
    have = suite.target.capabilities | suite.features
    out: dict[str, Scenario] = {}
    for s in _scenarios:
        if s.suite != suite.name or not s.needs <= have:
            continue
        if s.targets is not None and suite.target.name not in s.targets:
            continue
        if s.name not in out or s.targets is not None:
            out[s.name] = s
    return dict(sorted(out.items()))


@dataclass
class Step:
    step: int
    name: str
    start: str
    secs: int
    ok: bool
    problems: list[str]
    note: str


class Suite:
    """The hooks a kind of run provides around its scenarios"""

    name = ""

    def __init__(self, target: Target) -> None:
        self.target = target
        self.features: frozenset[str] = frozenset()

    def start(self, out: Path) -> None:
        """Before the first step (boot the player, set things up)"""

    def check(self, step: StepContext, outcome: Outcome) -> list[str]:
        """Problems found after a step, on top of the scenario's own"""
        return []

    def recover(self, step: StepContext, outcome: Outcome) -> bool:
        """After a lost player: collect what it left, get it back. False ends
        the run."""
        return False

    def finish(self, out: Path, steps: list[Step]) -> None:
        """After the last step (reports, ledgers)"""


@dataclass
class RunSettings:
    out: Path
    deadline: datetime
    seed: int
    only: list[str] | None = None
    weights: dict[str, int] = field(default_factory=dict)


def run(suite: Suite, s: RunSettings, echo: Callable[[str], None] = print) -> list[Step]:
    """Steps until S.deadline; the steps taken"""
    table = scenarios_for(suite)
    names = s.only or list(table)
    unknown = [n for n in names if n not in table]
    if unknown:
        raise SystemExit(f"no such {suite.name} scenario for {suite.target.name}: "
                         f"{', '.join(unknown)} (have {', '.join(table)})")
    pool = [n for n in names for _ in range(s.weights.get(n, table[n].weight))]
    if not pool:
        raise SystemExit("every scenario has weight 0")
    rng = random.Random(s.seed)
    s.out.mkdir(parents=True, exist_ok=True)
    start = datetime.now()
    echo(f"{suite.name} on {suite.target.name}: seed {s.seed}, until "
         f"{s.deadline:%Y-%m-%d %H:%M}, scenarios {', '.join(names)}")
    suite.start(s.out)
    steps: list[Step] = []
    with (s.out / "steps.jsonl").open("a") as log:
        while datetime.now() < s.deadline:
            name = rng.choice(pool)
            ctx = StepContext(len(steps) + 1, name, suite.target, suite, rng, s.out, start)
            t0 = time.time()
            try:
                outcome = table[name].fn(ctx)
            except Exception as e:
                outcome = Outcome([f"{suite.name} error {e!r}"],
                                  output=traceback.format_exc())
            if not outcome.lost:
                outcome.problems += suite.check(ctx, outcome)
            ctx.path(".txt").write_text(outcome.output)
            if outcome.sheet and outcome.sheet.exists():
                outcome.sheet.replace(ctx.path(".png"))
            st = Step(ctx.index, name, time.strftime("%H:%M:%S", time.localtime(t0)),
                      round(time.time() - t0), not outcome.problems and not outcome.lost,
                      (["LOST"] if outcome.lost else []) + outcome.problems, outcome.note)
            steps.append(st)
            log.write(json.dumps(asdict(st)) + "\n")
            log.flush()
            echo(f"{st.step:3d} {name:8} {'ok  ' if st.ok else 'FAIL'} {st.note} "
                 f"{'; '.join(st.problems)[:200]}".rstrip())
            if outcome.lost and not suite.recover(ctx, outcome):
                break
    suite.finish(s.out, steps)
    lost = bool(steps) and steps[-1].problems[:1] == ["LOST"]
    echo(f"done: {len(steps)} steps, {sum(not x.ok for x in steps)} failed"
         + (", player lost" if lost else ""))
    return steps


def deadline(hours: float | None = None, until: str | None = None) -> datetime:
    """--hours H or --until HH:MM (the next such time) as a datetime"""
    now = datetime.now()
    if until:
        hh, mm = (int(x) for x in until.split(":"))
        end = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if end <= now:
            end += timedelta(days=1)
        return end
    if hours is None:
        raise SystemExit("--hours or --until needed")
    return now + timedelta(hours=hours)


def load_steps(out: Path) -> list[dict[str, Any]]:
    """The steps.jsonl of an earlier run"""
    f = out / "steps.jsonl"
    return [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
