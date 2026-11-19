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

    @scenario("tone", suite="soak", area="audio", weight=2)
    def tone(step: StepContext) -> Outcome: ...

    @scenario("fm", suite="torture", area="fm", target={"yp3"})
    @scenario("sleep", suite="torture", area="lcd", needs={"lcd-gate"})

`area` is what the scenario tests, one of AREAS (bring-up's driver order,
then what builds on the drivers): `arb TARGET test AREA` runs an area's
scenarios once each (run_once), torture and soak draw from all of them at
random (run). `target` limits a scenario to those players (a player's own
beats a generic one of the same name); `needs` names target capabilities or
suite features (Suite.features), and a scenario is only in a run whose
target and suite have them all. Per step the run directory gets
`NNN-name.txt` (the scenario's output), a line in `steps.jsonl`, and
`NNN-name.png` when the suite makes contact sheets.

A scenario that cannot test anything this time (no music library on the
card, no Line In capture) returns an Outcome with `skipped` set: a step
that passes but answers nothing, counted apart in a test pass.
"""

from __future__ import annotations

import json
import random
import time
import traceback
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .target import Target

SAME_ERROR_STOP = 3     # steps in a row that raise the same error end a run
# What a scenario tests, in the order a test pass runs them: bring-up's
# drivers (skills/bring-up.md, Phase 3), then what builds on them
AREAS = ("boot", "lcd", "keys", "storage", "audio", "power", "usb", "rtc", "record", "fm",
         "bt", "playback", "ui", "plugins", "db")


@dataclass
class Outcome:
    """What a step found. LOST: the player did not come back"""

    problems: list[str] = field(default_factory=list)
    note: str = ""
    output: str = ""
    lost: bool = False
    sheet: Path | None = None       # a contact sheet to keep as NNN-name.png
    skipped: bool = False           # nothing to test this time (the note says why)


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
    area: str
    weight: int = 1
    targets: frozenset[str] | None = None
    needs: frozenset[str] = frozenset()


_scenarios: list[Scenario] = []
_kinds: set[str] = set()


def suite_kind(need: str) -> str:
    """NEED, registered as one that says which kind of suite a scenario is
    for (a Rockbox dev build's torture), not something a player may lack:
    left_out() does not list scenarios for another kind"""
    _kinds.add(need)
    return need


def scenario(name: str, *, suite: str, area: str, weight: int = 1,
             target: Iterable[str] | None = None,
             needs: Iterable[str] = ()) -> Callable[[ScenarioFn], ScenarioFn]:
    if area not in AREAS:
        raise ValueError(f"scenario {name}: area {area!r} is none of {', '.join(AREAS)}")

    def register(fn: ScenarioFn) -> ScenarioFn:
        _scenarios.append(Scenario(name, fn, suite, area, weight,
                                   None if target is None else frozenset(target),
                                   frozenset(needs)))
        return fn
    return register


def _for_target(suite: Suite) -> dict[str, Scenario]:
    """The suite's scenarios for its target, whatever they need; a player's
    own beat generic ones"""
    out: dict[str, Scenario] = {}
    for s in _scenarios:
        if s.suite != suite.name:
            continue
        if s.targets is not None and suite.target.name not in s.targets:
            continue
        if s.name not in out or s.targets is not None:
            out[s.name] = s
    return out


def scenarios_for(suite: Suite) -> dict[str, Scenario]:
    """The suite's scenarios for its target that it and its target have
    everything for"""
    have = suite.target.capabilities | suite.features
    return dict(sorted((n, s) for n, s in _for_target(suite).items() if s.needs <= have))


def left_out(suite: Suite) -> dict[str, str]:
    """The suite's scenarios for its target that miss something: why"""
    have = suite.target.capabilities | suite.features
    return {n: "needs " + ", ".join(sorted(s.needs - have))
            for n, s in sorted(_for_target(suite).items())
            if not s.needs <= have and not (s.needs - have) & _kinds}


def in_order(table: dict[str, Scenario]) -> list[str]:
    """TABLE's names in test order: by area (AREAS), then by name"""
    return sorted(table, key=lambda n: (AREAS.index(table[n].area), n))


@dataclass
class Step:
    step: int
    name: str
    start: str
    secs: int
    ok: bool
    problems: list[str]
    note: str
    skipped: bool = False


class Suite:
    """The hooks a kind of run provides around its scenarios. FEATURES: what
    this run of it has, on top of the target's capabilities (a player's
    torture suite declares its build's; an instance adds what its config
    allows)"""

    name = ""
    features: frozenset[str] = frozenset()

    def __init__(self, target: Target) -> None:
        self.target = target

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
    """Steps at random until S.deadline; the steps taken"""
    table = scenarios_for(suite)
    names = s.only or list(table)
    unknown = [n for n in names if n not in table]
    if unknown:
        raise SystemExit(f"no such {suite.name} scenario for {suite.target.name}: "
                         f"{', '.join(unknown)} (have {', '.join(table)})")
    def weight(n: str) -> int:
        # one named in --only runs even if off by default (weight 0), unless
        # --weights says 0
        w = s.weights.get(n, table[n].weight)
        return 1 if w == 0 and s.only and n not in s.weights else w

    pool = [n for n in names for _ in range(weight(n))]
    if not pool:
        raise SystemExit("every scenario has weight 0")
    rng = random.Random(s.seed)

    def picks() -> Iterator[str]:
        while datetime.now() < s.deadline:
            yield rng.choice(pool)
    return _steps(suite, s.out, table, picks(), rng=rng, echo=echo,
                  head=f"{suite.name} on {suite.target.name}: seed {s.seed}, until "
                  f"{s.deadline:%Y-%m-%d %H:%M}, scenarios "
                  f"{', '.join(n for n in names if n in pool)}")


def run_once(suite: Suite, out: Path, names: list[str], seed: int,
             echo: Callable[[str], None] = print) -> list[Step]:
    """NAMES in order, a step each (a test pass); the steps taken, fewer if
    the player was lost for good"""
    table = scenarios_for(suite)
    if unknown := [n for n in names if n not in table]:
        raise SystemExit(f"no such {suite.name} scenario for {suite.target.name}: "
                         f"{', '.join(unknown)}")
    return _steps(suite, out, table, iter(names), rng=random.Random(seed), echo=echo,
                  head=f"{suite.name} on {suite.target.name}: seed {seed}, "
                  f"{len(names)} steps: {' '.join(names)}")


def _steps(suite: Suite, out: Path, table: dict[str, Scenario], names: Iterator[str], *,
           rng: random.Random, echo: Callable[[str], None], head: str) -> list[Step]:
    out.mkdir(parents=True, exist_ok=True)
    start = datetime.now()
    echo(head)
    suite.start(out)
    steps: list[Step] = []
    errors: list[str] = []      # the errors of the last steps, when each raised one
    with (out / "steps.jsonl").open("a") as log:
        for name in names:
            ctx = StepContext(len(steps) + 1, name, suite.target, suite, rng, out, start)
            t0 = time.time()
            try:
                outcome = table[name].fn(ctx)
                errors.clear()
            except Exception as e:
                outcome = Outcome([f"{suite.name} error {e!r}"],
                                  output=traceback.format_exc())
                errors.append(repr(e))
            if not outcome.lost:
                outcome.problems += suite.check(ctx, outcome)
            ctx.path(".txt").write_text(outcome.output)
            if outcome.sheet and outcome.sheet.exists():
                outcome.sheet.replace(ctx.path(".png"))
            st = Step(ctx.index, name, time.strftime("%H:%M:%S", time.localtime(t0)),
                      round(time.time() - t0), not outcome.problems and not outcome.lost,
                      (["LOST"] if outcome.lost else []) + outcome.problems, outcome.note,
                      outcome.skipped)
            steps.append(st)
            log.write(json.dumps(asdict(st)) + "\n")
            log.flush()
            echo(f"{st.step:3d} {name:8} {verdict(st):4} {st.note} "
                 f"{'; '.join(st.problems)[:200]}".rstrip())
            if outcome.lost and not suite.recover(ctx, outcome):
                break
            if len(errors) >= SAME_ERROR_STOP and len(set(errors[-SAME_ERROR_STOP:])) == 1:
                echo(f"stopped: the same error {SAME_ERROR_STOP} steps running (the host's, "
                     "not the player's: a step that errors at once would spin)")
                break
    suite.finish(out, steps)
    lost = bool(steps) and steps[-1].problems[:1] == ["LOST"]
    echo(f"done: {len(steps)} steps, {sum(not x.ok for x in steps)} failed"
         + (", player lost" if lost else ""))
    return steps


def verdict(st: Step) -> str:
    return "FAIL" if not st.ok else "skip" if st.skipped else "ok"


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
