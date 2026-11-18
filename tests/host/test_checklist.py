"""The port checklist: the template reads back as written, and `check` and
`update` catch what a careless or hurried form would slip past."""

from __future__ import annotations

import unittest

from auto_rockbox import checklist
from auto_rockbox.checklist import Form, Item, check, parse, template, update


def close(form: Form, iid: str, state: str, evidence: str) -> None:
    _, it = form.items()[iid]
    it.state, it.evidence = state, [evidence]


class TestTemplate(unittest.TestCase):
    def test_round_trip(self) -> None:
        text = checklist.TEMPLATE.read_text().replace(checklist.PLACEHOLDER, "p1")
        assert parse(text).render() == text

    def test_shape(self) -> None:
        t = template("p1")
        ids = [it.id for ph in t.phases for it in ph.items]
        assert len(ids) == len(set(ids)), "an id twice"
        for ph in t.phases:
            for it in ph.items:
                assert it.id.startswith(ph.name + "."), it.id
                assert not it.closed and not any(it.evidence)
        assert check(t, t).errors == []


class TestCheck(unittest.TestCase):
    def test_evidence(self) -> None:
        t = template("p1")
        f = template("p1")
        close(f, "setup.backlog", "x", "")
        close(f, "setup.notes", "x", "works fine")
        close(f, "setup.tree", "x", "2026-11-18 branch p1")
        close(f, "setup.target", "x", "2026-11-18: targets/p1/__init__.py, abc1234")
        close(f, "drivers.fm", "-", "")
        close(f, "drivers.bt", "-", "n/a: no radio on the board")
        errs = check(f, t).errors
        assert "setup.backlog: done without evidence" in errs
        assert "setup.notes: evidence without a date (YYYY-MM-DD)" in errs
        assert any(e.startswith("setup.tree: evidence without anything to open") for e in errs)
        assert not any(e.startswith(("setup.target", "drivers.bt")) for e in errs)
        assert any(e.startswith("drivers.fm: not applicable") for e in errs)

    def test_weakened_or_missing(self) -> None:
        t = template("p1")
        f = template("p1")
        _, it = f.items()["wayback.backup"]
        it.text = "flash dumped once"
        f.phases[0].items.pop(0)
        f.phases[0].items.append(Item(" ", "setup.extra", "an item of the port's own"))
        f.phases[0].items.append(Item(" ", "x.gestures", "edge swipes"))
        errs = check(f, t).errors
        assert "wayback.backup: text differs from the template's" in errs
        assert any(e.startswith(f"{t.phases[0].items[0].id}: missing") for e in errs)
        assert any(e.startswith("setup.extra: not in the template") for e in errs)
        assert not any(e.startswith("x.gestures") for e in errs)

    def test_through(self) -> None:
        t = template("p1")
        f = template("p1")
        for it in f.phases[0].items:
            it.state, it.evidence = "x", ["2026-11-18 docs/p1/TODO.md"]
        assert check(f, t, t.phases[0].name).errors == []
        errs = check(f, t, t.phases[1].name).errors
        assert errs and all(": open, and --through" in e for e in errs)
        with self.assertRaises(checklist.ChecklistError):
            check(f, t, "nosuchphase")


class TestUpdate(unittest.TestCase):
    def test_keeps_states_adds_items(self) -> None:
        t = template("p1")
        old = template("p1")
        close(old, "wayback.backup", "x", "2026-11-18 stock/p1.bin")
        removed = old.phases[2].items.pop()          # an item a newer template added
        old.phases[2].items.append(Item("x", "wayback.gone", "an item since dropped",
                                        ["2026-11-01 a1b2c3d"]))
        old.phases[1].items.append(Item(" ", "x.own", "the port's own"))
        new, notes = update(parse(old.render()), t)
        assert check(new, t).errors == []
        items = new.items()
        assert items["wayback.backup"][1].state == "x"
        assert items[removed.id][1].state == " " and f"{removed.id}: new, open" in notes
        assert "wayback.gone" not in items and any(n.startswith("wayback.gone") for n in notes)
        assert items["x.own"][0].name == old.phases[1].name
