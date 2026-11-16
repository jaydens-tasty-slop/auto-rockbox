"""`arb TARGET test`: one pass of every scenario the player qualifies for,
each once, in bring-up order (engine.AREAS), with a verdict per scenario.
Soak's scenarios come first (each a whole boot, no dev shell needed, so they
serve a player still in bring-up), then torture's over the dev shell (one
boot, with a torture run's health checks and crash collection). Torture and
soak runs draw from the same scenarios at random; a test pass is what a
change, a newly brought-up driver or a release candidate gets through first
(skills/testing.md).

    arb yp3 test build/test-elf/rockbox.elf build/test/RUN              # all
    arb yp3 test build/test-elf/rockbox.elf build/test/RUN storage audio

OUT gets soak/ and torture/ (run directories as those runs write them) and
summary.md: a line per step (area, suite, verdict, note), then the
scenarios left out and why. A pass uses seed 1 unless --seed says
otherwise, so two builds get the same choices.
"""

from __future__ import annotations

import sys
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .. import engine
from ..engine import AREAS, Scenario, Step, Suite
from ..errors import UsageError
from ..registry import Context, arg, command
from ..run import parse_settings
from .soak import SoakSuite
from .torture import TortureSuite, suite_class


@dataclass
class Plan:
    """One suite's part of a pass: the steps in order, those left out and why"""

    suite: Suite
    table: dict[str, Scenario]
    names: list[str]
    left_out: dict[str, str]


def plan(suite: Suite, areas: set[str], only: set[str], repeat: int) -> Plan:
    """SUITE's scenarios in AREAS (all if none), or those ONLY names, each
    REPEAT times; one of weight 0 (off by default) only when named"""
    table = engine.scenarios_for(suite)
    wanted = [n for n in engine.in_order(table)
              if (n in only if only else
                  table[n].weight > 0 and (not areas or table[n].area in areas))]
    left = {n: why for n, why in engine.left_out(suite).items() if not only or n in only}
    if not only:
        left |= {n: f"off by default (--only {n})" for n, s in table.items()
                 if s.weight == 0 and (not areas or s.area in areas)}
    return Plan(suite, table, [n for n in wanted for _ in range(repeat)], left)


@dataclass(frozen=True)
class Row:
    area: str
    suite: str
    step: Step


def summary(rows: list[Row], plans: list[Plan], head: str) -> list[str]:
    """summary.md's lines"""
    def cell(text: str) -> str:
        return text.replace("|", "/").replace("\n", " ")[:200]
    out = [f"# {head}", "", "| area | scenario | suite | verdict | secs | note, problems |",
           "|---|---|---|---|---|---|"]
    for r in rows:
        st = r.step
        out.append(f"| {r.area} | {st.name} | {r.suite} | {engine.verdict(st)} | {st.secs} | "
                   f"{cell('; '.join(filter(None, [st.note, *st.problems])))} |")
    failed = sum(not r.step.ok for r in rows)
    skipped = sum(r.step.ok and r.step.skipped for r in rows)
    left = [(p.suite.name, n, why) for p in plans for n, why in sorted(p.left_out.items())]
    out += ["", f"{len(rows) - failed - skipped} passed, {failed} failed, {skipped} skipped "
            f"(nothing to test), {len(left)} not run"]
    if missing := [n for p in plans for n in dict.fromkeys(p.names)
                   if n not in {r.step.name for r in rows if r.suite == p.suite.name}]:
        out.append(f"never reached (the player was lost): {', '.join(missing)}")
    out += [f"- {n} ({suite}): {why}" for suite, n, why in left]
    return out


@command("test", help="every scenario the player qualifies for, once each: pass, fail or skip",
         args=[
             arg("image", type=Path, help="the dev build's rockbox.elf, in a directory of its "
                 "own: every recovery boots that file again"),
             arg("out", type=Path, help="run directory: soak/, torture/, summary.md"),
             arg("areas", nargs="*", metavar="AREA",
                 help=f"only these areas (default all): {' '.join(AREAS)}"),
             arg("--only", help="comma-separated scenarios (one off by default runs too)"),
             arg("--repeat", type=int, default=1, help="steps of each scenario (default 1)"),
             arg("--seed", type=int, default=1, help="the random choices' seed (default 1)"),
             arg("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE",
                 help="soak: a run setting for every boot (e.g. volume=-15)"),
             arg("--shots", action="store_true",
                 help="soak: a contact sheet (framebuffer + webcam) per step"),
             arg("--after-boot", action="append", default=[], metavar="COMMAND",
                 help="torture: a shell command after every boot (repeatable)"),
             arg("--no-boot", action="store_true",
                 help="torture: the image already runs with its shell: start on it"),
         ])
def test_cmd(ctx: Context, args: Namespace) -> int:
    """Runs the soak scenarios, then the torture scenarios (with a dev shell),
    of the AREAs given, each once. Exit status 1 if any step failed."""
    if not args.image.is_file():
        raise UsageError(f"{args.image}: no such image")
    if bad := [a for a in args.areas if a not in AREAS]:
        raise UsageError(f"no such area: {', '.join(bad)} (have {' '.join(AREAS)})")
    t, areas = ctx.t, set(args.areas)
    only = set(args.only.split(",")) if args.only else set()
    suites: list[Suite] = [SoakSuite(t, args.image, args.shots, parse_settings(args.settings))]
    if t.has("shell"):
        suites.append(suite_class(t.name)(t, args.image, boot=not args.no_boot,
                                          after_boot=args.after_boot, seed=args.seed))
    try:
        plans = [plan(s, areas, only, args.repeat) for s in suites]
        if unknown := only - {n for p in plans for n in p.table}:
            raise UsageError(f"no such scenario for {t.name}: {', '.join(sorted(unknown))}")
        head = (f"arb {t.name} test {' '.join(args.areas) or 'all areas'}: "
                f"{datetime.now():%Y-%m-%d %H:%M}, seed {args.seed}, {args.image}")
        if not t.has("shell"):
            head += " (no dev shell: soak scenarios only)"
        rows: list[Row] = []
        for p in plans:
            if p.names:
                steps = engine.run_once(p.suite, args.out / p.suite.name, p.names, args.seed,
                                        echo=lambda line: print(line, flush=True))
                rows += [Row(p.table[st.name].area, p.suite.name, st) for st in steps]
    finally:
        for s in suites:
            if isinstance(s, TortureSuite):
                s.close()
    lines = summary(rows, plans, head)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    if not rows:
        print("nothing ran", file=sys.stderr)
    return 0 if rows and all(r.step.ok for r in rows) else 1
