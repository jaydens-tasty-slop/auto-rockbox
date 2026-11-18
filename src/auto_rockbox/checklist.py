"""The port checklist: auto-rockbox's procedure as a form each port copies
and fills in as it goes (port-checklist.md, skills/bring-up.md "The port's
checklist"), so a merge request can show the procedure was followed. The
commands are in commands/checklist.py.

    arb checklist init ts28                 # docs/ts28/checklist.md from the template
    arb checklist check ts28 --through devloop
    arb checklist update docs/ts28/checklist.md

A form is markdown: phases are `## NAME: title` sections, items are
`- [ ] ID text` lines with an indented `evidence:` line under each. `[x]` is
done, `[-]` not applicable, `[ ]` open. `check` holds the form against the
template: every template item present with the template's text, ids of the
port's own starting `x.`, evidence on every closed item, and with
--through, nothing open up to that phase.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ArbError
from .workspace import Workspace

TEMPLATE = Path(__file__).with_name("port-checklist.md")
PLACEHOLDER = "PLAYER"
HEADER = re.compile(r"<!-- arb-checklist v(\d+) target=(\S+) -->")
PHASE = re.compile(r"^## ([a-z][a-z0-9-]*): (.+)$")
ITEM = re.compile(r"^- \[([ x-])\] ([a-z][a-z0-9-]*(?:\.[a-z0-9-]+)+) (.+)$")
EVIDENCE = re.compile(r"^  evidence:(.*)$")
CONTINUATION = re.compile(r"^    (\S.*)$")
DATE = re.compile(r"\b20\d\d-[01]\d-[0-3]\d\b")
# Something a reviewer can open: a commit, a path or file, a fact or
# question id (F-0112, L-F-0003, IQ-012), a URL
REFERENCE = re.compile(r"\b[0-9a-f]{7,40}\b|\S/\S|\S\.(?:md|txt|json|png|log|csv|c|h|S|py)\b"
                       r"|\b(?:[A-Z]+-)*[A-Z]+-\d{3,}\b|https?://")
OWN = "x."

STATES = {" ": "open", "x": "done", "-": "n/a"}


class ChecklistError(ArbError):
    """A form that can't be read or written"""


@dataclass
class Item:
    state: str          # " ", "x" or "-"
    id: str
    text: str
    evidence: list[str] = field(default_factory=list)  # its line, then continuations

    @property
    def closed(self) -> bool:
        return self.state != " "

    def render(self) -> list[str]:
        ev = self.evidence or [""]
        first = f"  evidence: {ev[0]}" if ev[0] else "  evidence:"
        return [f"- [{self.state}] {self.id} {self.text}", first, *(f"    {e}" for e in ev[1:])]


@dataclass
class Phase:
    name: str
    title: str
    intro: list[str] = field(default_factory=list)   # lines between the heading and the items
    items: list[Item] = field(default_factory=list)


@dataclass
class Form:
    target: str
    version: int
    preamble: list[str]
    phases: list[Phase]

    def items(self) -> dict[str, tuple[Phase, Item]]:
        return {it.id: (ph, it) for ph in self.phases for it in ph.items}

    def render(self) -> str:
        out = list(self.preamble)
        for ph in self.phases:
            out += [f"## {ph.name}: {ph.title}", "", *ph.intro, *([""] if ph.intro else [])]
            for it in ph.items:
                out += it.render()
            out.append("")
        while out and not out[-1]:
            out.pop()
        return "\n".join(out) + "\n"


def parse(text: str, source: str = "form") -> Form:
    lines = text.splitlines()
    head = next((m for line in lines if (m := HEADER.search(line))), None)
    if head is None:
        raise ChecklistError(f"{source}: no '<!-- arb-checklist vN target=NAME -->' line")
    preamble: list[str] = []
    phases: list[Phase] = []
    item: Item | None = None
    for n, line in enumerate(lines, 1):
        if m := PHASE.match(line):
            phases.append(Phase(m[1], m[2]))
            item = None
        elif not phases:
            preamble.append(line)
        elif m := ITEM.match(line):
            item = Item(m[1], m[2], m[3].strip())
            phases[-1].items.append(item)
        elif item is not None and (m := EVIDENCE.match(line)):
            item.evidence = [m[1].strip()]
        elif item is not None and item.evidence and (m := CONTINUATION.match(line)):
            item.evidence.append(m[1].strip())
        elif line.startswith("- ["):
            raise ChecklistError(f"{source}:{n}: not an item line (`- [ ] phase.name text`)")
        elif not phases[-1].items:
            phases[-1].intro.append(line)
        elif line.strip():
            raise ChecklistError(f"{source}:{n}: stray line under an item: {line.strip()[:60]}")
    for ph in phases:   # the blank lines around the intro are the renderer's
        while ph.intro and not ph.intro[-1].strip():
            ph.intro.pop()
        while ph.intro and not ph.intro[0].strip():
            ph.intro.pop(0)
    return Form(head[2], int(head[1]), preamble, phases)


def template(target: str) -> Form:
    return parse(TEMPLATE.read_text().replace(PLACEHOLDER, target), str(TEMPLATE))


def default_path(ws: Workspace, target: str) -> Path:
    return ws.target(target).path("checklist", f"docs/{target}/checklist.md")


def resolve(ws: Workspace, what: str) -> Path:
    """WHAT as a file, or as a player whose form is at its configured path"""
    p = Path(what)
    return p if p.suffix == ".md" or p.exists() else default_path(ws, what)


def evidence_problems(it: Item) -> list[str]:
    text = " ".join(it.evidence).strip()
    if it.state == "-":
        return [] if text.startswith("n/a:") and len(text) > len("n/a:") + 3 else \
            ["not applicable without `evidence: n/a: <why>`"]
    if it.state != "x":
        return []
    if not text:
        return ["done without evidence"]
    out = []
    if not DATE.search(text):
        out.append("evidence without a date (YYYY-MM-DD)")
    if not REFERENCE.search(text):
        out.append("evidence without anything to open (a commit, path, fact id or URL)")
    return out


@dataclass
class Report:
    errors: list[str]
    rows: list[tuple[str, int, int, int]]   # phase, done, n/a, open

    def lines(self) -> list[str]:
        out = [f"{'phase':10} {'done':>5} {'n/a':>5} {'open':>5}"]
        out += [f"{p:10} {d:5} {na:5} {o:5}" for p, d, na, o in self.rows]
        out += [f"error: {e}" for e in self.errors]
        out.append("ok" if not self.errors else f"{len(self.errors)} errors")
        return out


def check(form: Form, tmpl: Form, through: str | None = None) -> Report:
    """FORM against TMPL; with THROUGH, every item up to that phase closed"""
    errors: list[str] = []
    order = [ph.name for ph in tmpl.phases]
    if through is not None and through not in order:
        raise ChecklistError(f"--through {through}: not a phase ({', '.join(order)})")
    if form.version != tmpl.version:
        errors.append(f"form v{form.version}, template v{tmpl.version}: arb checklist update")
    have = form.items()
    seen: set[str] = set()
    for ph in form.phases:
        if ph.name not in order:
            errors.append(f"## {ph.name}: not a phase of the template")
        for it in ph.items:
            if it.id in seen:
                errors.append(f"{it.id}: listed twice")
            seen.add(it.id)
    for tph in tmpl.phases:
        for t in tph.items:
            if t.id not in have:
                errors.append(f"{t.id}: missing (arb checklist update brings it in)")
                continue
            ph, it = have[t.id]
            if ph.name != tph.name:
                errors.append(f"{t.id}: under ## {ph.name}, the template's is ## {tph.name}")
            if it.text != t.text:
                errors.append(f"{t.id}: text differs from the template's")
    tids = {t.id for tph in tmpl.phases for t in tph.items}
    rows = []
    last = order.index(through) if through is not None else -1
    for ph in form.phases:
        for it in ph.items:
            if it.id not in tids and not it.id.startswith(OWN):
                errors.append(f"{it.id}: not in the template (the port's own items start {OWN})")
            errors += [f"{it.id}: {p}" for p in evidence_problems(it)]
            if (ph.name in order and order.index(ph.name) <= last and not it.closed):
                errors.append(f"{it.id}: open, and --through {through} needs it closed")
        rows.append((ph.name, sum(i.state == "x" for i in ph.items),
                     sum(i.state == "-" for i in ph.items), sum(not i.closed for i in ph.items)))
    return Report(errors, rows)


def update(form: Form, tmpl: Form) -> tuple[Form, list[str]]:
    """FORM rebuilt on TMPL: the template's phases, items and texts, with the
    form's states and evidence, and its own (x.) items kept in their phases.
    Also the notes on what changed."""
    have = form.items()
    notes = []
    phases = []
    for tph in tmpl.phases:
        items = []
        for t in tph.items:
            if t.id in have:
                _, it = have[t.id]
                if it.text != t.text:
                    notes.append(f"{t.id}: text from the template")
                items.append(Item(it.state, t.id, t.text, it.evidence))
            else:
                notes.append(f"{t.id}: new, open")
                items.append(Item(" ", t.id, t.text))
        items += [it for ph in form.phases if ph.name == tph.name
                  for it in ph.items if it.id.startswith(OWN)]
        phases.append(Phase(tph.name, tph.title, tph.intro, items))
    tids = {t.id for tph in tmpl.phases for t in tph.items}
    for iid, (ph, it) in have.items():
        if iid not in tids and not iid.startswith(OWN):
            notes.append(f"{iid}: dropped, no longer in the template "
                         f"(was [{it.state}] {' '.join(it.evidence)[:60]})")
        elif iid.startswith(OWN) and ph.name not in {p.name for p in tmpl.phases}:
            notes.append(f"{iid}: dropped with its phase ## {ph.name}")
    preamble = [HEADER.sub(f"<!-- arb-checklist v{tmpl.version} target={form.target} -->", line)
                for line in form.preamble]
    return Form(form.target, tmpl.version, preamble, phases), notes
