"""`arb checklist`: a player's copy of the port checklist (auto_rockbox.checklist)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from ..checklist import ChecklistError, check, default_path, parse, resolve, template, update
from ..registry import Context, arg, command, group

group("checklist", help="the port checklist: start a player's form, check it, update it")


@command("checklist init", target=False, help="copy the template into the port as a player's form",
         args=[arg("player", help="the player's arb name, e.g. ts28"),
               arg("file", type=Path, nargs="?",
                   help="default: [targets.PLAYER] checklist, or docs/PLAYER/checklist.md")])
def init_cmd(ctx: Context, args: Namespace) -> int:
    out: Path = args.file or default_path(ctx.ws, args.player)
    if out.exists():
        raise ChecklistError(f"{out} exists: `arb checklist update {out}` brings in new items")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(template(args.player).render())
    print(f"{out}: the {args.player} checklist, every item open")
    return 0


@command("checklist check", target=False,
         help="hold a form against the template: items, evidence, and nothing open up to a phase",
         args=[arg("form", help="the form's path, or a player name (its default path)"),
               arg("--through", metavar="PHASE",
                   help="every item up to and including PHASE must be done or n/a")])
def check_cmd(ctx: Context, args: Namespace) -> int:
    path = resolve(ctx.ws, args.form)
    if not path.is_file():
        raise ChecklistError(f"{path}: no form (arb checklist init PLAYER)")
    form = parse(path.read_text(), str(path))
    report = check(form, template(form.target), args.through)
    print("\n".join(report.lines()))
    return 1 if report.errors else 0


@command("checklist update", target=False,
         help="bring a form up to the template: new items, current texts, states kept",
         args=[arg("form", help="the form's path, or a player name (its default path)")])
def update_cmd(ctx: Context, args: Namespace) -> int:
    path = resolve(ctx.ws, args.form)
    form = parse(path.read_text(), str(path))
    new, notes = update(form, template(form.target))
    path.write_text(new.render())
    print("\n".join(notes) or "up to date")
    return 0
