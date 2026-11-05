"""Pad coverage: a small synthetic tree and build (maps, an archive, a shared
adapter, a manifest with a glob and a source row), the gate on a scratch git
repository, and, with the port's checkout around, the real tree and builds
against yp3dev/padcheck.py."""

from __future__ import annotations

import contextlib
import csv
import importlib
import io
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import patch

from auto_rockbox import padcheck
from tests import Skip

PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
YP3DEV = PORT / "yp3dev"
TREE = PORT / "rockbox"
MANIFEST = PORT / "docs/plugin-pad-buttons.csv"

PAD = padcheck.PAD
SOURCES = {
    "apps/plugins/lib/pluginlib_actions.c":
        f"#include \"helper.h\"\nint pluginlib_getaction(int t)\n{{\n#ifdef {PAD}\n"
        "    return BUTTON_PAD_A;\n#endif\n    return button_get_w_tmo(t);\n}\n",
    "apps/plugins/lib/helper.h": "/* no input here */\n#define HELPER 1\n",
    "apps/plugins/lib/display_text.c": "void wait(void)\n{\n    button_get(true);\n}\n",
    "apps/plugins/padded.c":
        f"#include \"lib/helper.h\"\nvoid go(void) {{\n    int b = button_get(true);\n"
        f"#ifdef {PAD}\n    if (b == BUTTON_PAD_A) return;\n#endif\n}}\n",
    "apps/plugins/plain.c": "int f(void) { return 1; }  // button_get( in a comment\n",
    "apps/plugins/usepla.c": "void g(void)\n{\n    pluginlib_getaction(0, pla_main_ctx, 1);\n}\n",
    "apps/plugins/menu.c":
        "int button_get(bool block);\nvoid h(void)\n{\n    get_action(0, 0);\n}\n",
    "apps/plugins/fam/adapter.c": f"void a(void)\n{{\n    button_status();\n#if defined({PAD})\n"
                                  "    BUTTON_PAD_MENU;\n#endif\n}}\n",
    "apps/plugins/fam/one.c": "void one(void) {}\n",
    "apps/plugins/fam/two.c": "void two(void) {}\n",
}
# rock: its objects (an archive member as "lib/libplugin.a(name.o)"), under apps/plugins
ROCKS = {
    "padded": ["padded.o", "lib/libplugin.a(display_text.o)"],
    "plain": ["plain.o"],
    "usepla": ["usepla.o", "lib/libplugin.a(pluginlib_actions.o)"],
    "menu": ["menu.o"],
    "fam/one": ["fam/one.o", "fam/adapter.o", "lib/libplugin.a(pluginlib_actions.o)"],
    "fam/two": ["fam/two.o", "fam/adapter.o"],
}
HEADER = ["rock", "status", *padcheck.BUTTONS, "unpadded", "notes"]
ROWS = [
    {"rock": "padded", "status": "mapped", "a": "jump", "menu": "quit"},
    {"rock": "plain", "status": "none", "notes": "reads no input"},
    {"rock": "usepla", "status": "pla", "a": "select", "menu": "exit"},
    {"rock": "menu", "status": "core", "notes": "lists"},
    {"rock": "fam/*", "status": "shared", "a": "A", "menu": "pause"},
    {"rock": "lib/display_text.c", "status": "unpadded", "notes": "waits for any key"},
]


def make_tree(root: Path) -> tuple[Path, Path]:
    """The synthetic tree and its build under ROOT"""
    src, build = root / "src", root / "build"
    for rel, text in SOURCES.items():
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text(text)
    plugins = build / "apps/plugins"
    for rel in SOURCES:
        if rel.endswith(".c"):
            o = build / Path(rel).with_suffix(".o")
            o.parent.mkdir(parents=True, exist_ok=True)
            o.write_bytes(b"")
    for rock, objs in ROCKS.items():
        (plugins / f"{rock}.rock").write_bytes(b"")
        (plugins / f"{rock}.map").write_text("".join(
            f" .text  0x{i * 16:08x}  0x10 {plugins}/{o}\n" for i, o in enumerate(objs)))
    return src, build


def write_manifest(path: Path, rows: list[dict[str, str]]) -> list[padcheck.Row]:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, HEADER)
        w.writeheader()
        w.writerows(rows)
    return padcheck.load_manifest(path)


def checked(root: Path, rows: list[dict[str, str]], only: bool = False) -> padcheck.Report:
    src, build = make_tree(root)
    return padcheck.check(padcheck.membership(build, src), src,
                          write_manifest(root / "m.csv", rows), only)


def test_membership() -> None:
    with tempfile.TemporaryDirectory() as d:
        src, build = make_tree(Path(d))
        rocks = padcheck.facts(padcheck.membership(build, src), src)
    assert sorted(rocks) == sorted(ROCKS)
    padded = rocks["padded"]
    assert padded.map == "apps/plugins/padded.map"
    assert padded.sources == ["apps/plugins/lib/display_text.c", "apps/plugins/lib/helper.h",
                              "apps/plugins/padded.c"], padded.sources
    assert padded.own == ["apps/plugins/lib/display_text.c", "apps/plugins/padded.c"]
    assert padded.own_pad == ["apps/plugins/padded.c"]
    assert padded.calls == {"apps/plugins/lib/display_text.c": ["button_get"],
                            "apps/plugins/padded.c": ["button_get"]}
    assert rocks["plain"].calls == {}                       # a comment is no call
    assert rocks["menu"].calls == {"apps/plugins/menu.c": ["get_action"]}   # nor a declaration
    assert rocks["usepla"].pla_ref and not rocks["padded"].pla_ref
    one = rocks["fam/one"]
    assert one.shared_input == one.shared_pad == ["apps/plugins/fam/adapter.c",
                                                  padcheck.PLA], one
    lines = padcheck.inventory(rocks)
    assert lines[0] == "6 rocks; 3 read input in their own sources", lines
    assert "  apps/plugins/fam/adapter.c (2 rocks)" in lines


def test_check_passes() -> None:
    with tempfile.TemporaryDirectory() as d:
        r = checked(Path(d), ROWS)
    assert r.problems == [], r.problems
    assert r.status == 0
    assert r.summary == ("6 of 6 rocks checked, 6 rows: 1 mapped, 2 shared, 1 pla, 1 core, "
                         "1 none, 0 exempt; 0 problem(s)"), r.summary


def test_check_problems() -> None:
    rows = [{**ROWS[0], "status": "shared"},                # padded has pad code of its own
            {"rock": "plain", "status": "mapped", "a": "x", "menu": "y"},
            {"rock": "usepla", "status": "pla", "a": "select"},
            {"rock": "menu", "status": "core"},
            {"rock": "fam/*", "status": "shared", "a": "A", "menu": "pause"},
            {"rock": "fam/tw?", "status": "none"},          # longer, so it wins for fam/two
            {"rock": "gone", "status": "none"},
            {"rock": "lib/display_text.c", "status": "unpadded"}]
    with tempfile.TemporaryDirectory() as d:
        r = checked(Path(d), rows)
    assert r.problems == [
        "gone: manifest row matches no built rock",
        "lib/display_text.c: a source row needs status unpadded and notes",
        "fam/two: none, but reads input in apps/plugins/fam/adapter.c",
        "menu: core needs a note naming the screens",
        "padded: has HAVE_BLUETOOTH_HID code of its own: mapped",
        "padded: shared, but no shared input code it links has pad code",
        "plain: mapped, but none of its own sources has HAVE_BLUETOOTH_HID code",
        "usepla: pla, but no meaning given for menu",
    ], r.problems
    assert r.status == 1


def test_check_unexcused_and_only() -> None:
    with tempfile.TemporaryDirectory() as d:
        r = checked(Path(d), [*ROWS[:4], ROWS[4] | {"rock": "fam/one"}])
        only = checked(Path(d), [*ROWS[:4], ROWS[4] | {"rock": "fam/one"}], only=True)
    assert r.problems == ["fam/two: no manifest row",
                          "padded: reads input without pad code in "
                          "apps/plugins/lib/display_text.c"], r.problems
    assert only.problems == r.problems[1:]


def test_glob_clash() -> None:
    rows: list[padcheck.Row] = [{"rock": "fam/*e", "status": "none"},
                                {"rock": "fam/o*", "status": "none"}]
    found, unused, errors = padcheck.match(rows, ["fam/one"])
    assert errors == ["fam/one: matched by both fam/*e and fam/o*"]
    assert found["fam/one"] is rows[0] and unused == [rows[1]]


def test_puzzle_map() -> None:
    with tempfile.TemporaryDirectory() as d:
        puzzles = Path(d) / "puzzles"
        (puzzles / "src/unfinished").mkdir(parents=True)
        (puzzles / "src/unfinished/group.map").write_text("")
        assert padcheck.rock_map(puzzles / "sgt-group.rock") == \
            puzzles / "src/unfinished/group.map"
        assert padcheck.rock_map(puzzles / "other.rock") is None


def test_pad_cond() -> None:
    assert padcheck.pad_cond("ifdef", PAD) == 1
    assert padcheck.pad_cond("ifndef", PAD) == -1
    assert padcheck.pad_cond("ifdef", "FOO") == 0
    assert padcheck.pad_cond("if", f"defined({PAD}) && CONFIG_KEYPAD == X") == 1
    assert padcheck.pad_cond("if", f"!defined {PAD}") == -1
    assert padcheck.pad_cond("if", f"defined({PAD}) || defined(FOO)") is None
    assert padcheck.pad_cond("if", "FOO > 1") == 0


def test_off_view() -> None:
    text = (f"a\n#ifdef {PAD}\npad1 \\\n  more\n#else\nnot_pad\n#endif\nb\n"
            f"#if CONFIG_KEYPAD == X\n#ifdef {PAD}\nkeypad_pad\n#endif\n#endif\n"
            f"#ifndef {PAD}\noff_only\n#else\npad2\n#endif\n")
    v = padcheck.off_view(text)
    assert v.kept == ["a", "not_pad", "b", "#if CONFIG_KEYPAD == X", "#endif", "off_only", ""]
    assert [(n, line, kp) for n, line, kp in v.on] == [
        (3, "pad1 \\", False), (4, "  more", False), (11, "keypad_pad", True), (17, "pad2", False)]
    assert v.problems == []
    odd = padcheck.off_view(f"#if defined({PAD}) || X\n#elif {PAD}\n#endif\n#endif\n#if 1\n")
    assert odd.problems == [f"1: unclear {PAD} condition: defined({PAD}) || X",
                            f"2: #elif in or on a {PAD} conditional",
                            "4: #endif without #if", "unterminated #if"], odd.problems


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True)


def test_gate() -> None:
    if not shutil.which("git"):
        raise Skip("no git")
    with tempfile.TemporaryDirectory() as d:
        repo = Path(d)
        game = repo / "apps/plugins/game.c"
        game.parent.mkdir(parents=True)
        base = "switch (b) {\ncase BUTTON_SELECT:\n    fire();\n}\n"
        game.write_text(base)
        (repo / "apps/plugins/other.c").write_text("int x;\n")
        git(repo, "init", "-q")
        git(repo, "add", ".")
        git(repo, "commit", "-q", "-m", "base")
        game.write_text(f"switch (b) {{\ncase BUTTON_SELECT:\n#ifdef {PAD}\n"
                        "case BUTTON_PAD_A:\n#endif\n    fire();\n}\n")
        r = padcheck.gate(repo, "HEAD")
        assert (r.problems, r.warnings) == ([], []), r
        assert r.summary == "1 changed file(s) since HEAD: 0 problem(s), 0 warning(s)"

        game.write_text(f"switch (b) {{\ncase BUTTON_SELECT:\n#ifdef {PAD}\n"
                        "case BUTTON_LEFT: /* BUTTON_UP */\n#endif\n#if CONFIG_KEYPAD == X\n"
                        f"#ifdef {PAD}\ncase BUTTON_PAD_B:\n#endif\n#endif\n    fire(1);\n}}\n")
        (repo / "apps/plugins/new.c").write_text("int y;\n")
        r = padcheck.gate(repo, "HEAD", ["game.c", "new.c"])
        assert r.problems == [
            "apps/plugins/new.c: new file (pad code goes into the existing ones)",
            f"apps/plugins/game.c: without {PAD} it differs from HEAD:\n"
            "    @@ -3 +3,3 @@\n    -    fire();\n    +#if CONFIG_KEYPAD == X\n"
            "    +#endif\n    +    fire(1);",
            "apps/plugins/game.c:4: pad code names target buttons: BUTTON_LEFT",
        ], r.problems
        assert r.warnings == ["apps/plugins/game.c:8: 1 line(s) of pad code under a "
                              "CONFIG_KEYPAD conditional (only some keypads get it)"]
        assert r.lines()[-2].startswith("warning: apps/plugins/game.c:8")
        assert r.status == 1


# The port's own tree and builds, against yp3dev/padcheck.py


def _old() -> Any:
    """yp3dev/padcheck.py as a module"""
    if not (YP3DEV / "padcheck.py").exists() or not MANIFEST.exists() or not TREE.is_dir():
        raise Skip("no rockbox-yp3 checkout with yp3dev/padcheck.py and a Rockbox tree")
    if str(YP3DEV) not in sys.path:
        sys.path.insert(0, str(YP3DEV))
    with patch.object(sys, "dont_write_bytecode", True):      # nothing written there
        return importlib.import_module("padcheck")


def _printed(fn: Any, *args: Any) -> tuple[int, str]:
    """What an old command printed, and its exit status"""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = fn(*args)
    return rc, out.getvalue()


def _text(report: padcheck.Report) -> tuple[int, str]:
    """What `arb plugins` prints of REPORT, and its exit status"""
    return report.status, "".join(f"{line}\n" for line in report.lines())


def _all_plugin_build() -> Path:
    builds = [p.parents[2] for p in PORT.glob("build/*/apps/plugins/puzzles")
              if (p.parent / "rockblox.map").exists()]
    if not builds:
        raise Skip("no all-plugin build in the port's build/")
    return max(builds, key=lambda b: (b / "apps/plugins/rockblox.map").stat().st_mtime)


def test_real_small_build() -> None:
    """The dev build's own few rocks: inventory and check as the old script"""
    old = _old()
    build = PORT / "build/yp3"
    if not (build / "apps/plugins/rockblox.map").exists():
        raise Skip("no build/yp3 with its plugins")
    rocks = padcheck.facts(padcheck.membership(build, TREE), TREE)
    assert {n: asdict(r) for n, r in rocks.items()} == old.facts(old.membership(build, TREE),
                                                                 TREE)
    report = padcheck.check(padcheck.membership(build, TREE), TREE,
                            padcheck.load_manifest(MANIFEST), True)
    assert _text(report) == _printed(old.check, old.membership(build, TREE), TREE, MANIFEST, True)


def test_real_all_plugins() -> None:
    """An all-plugin build's check: the old one fed this one's membership"""
    old = _old()
    build = _all_plugin_build()
    rocks = padcheck.membership(build, TREE)
    as_old = {n: {"map": r.map, "sources": r.sources} for n, r in rocks.items()}
    report = padcheck.check(rocks, TREE, padcheck.load_manifest(MANIFEST))
    assert _text(report) == _printed(old.check, as_old, TREE, MANIFEST), build


def test_real_gate() -> None:
    old = _old()
    for base, paths in (("HEAD~10", []), ("HEAD~40", ["lib", "puzzles"])):
        assert _text(padcheck.gate(TREE, base, paths)) == _printed(old.gate, TREE, base, paths), \
            base
