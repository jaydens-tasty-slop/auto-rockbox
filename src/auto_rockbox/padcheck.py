"""Game-controller (BUTTON_PAD_*) coverage of a Rockbox tree's plugins.

Every .rock of an all-plugin build (configure without --plugins, make
rocks), from its linker map: its sources and the local headers they include,
its own (in it alone) and shared ones (in several rocks: libplugin, family
adapters), and which of them read input. `check` holds that to a manifest;
`gate` checks that every change to apps/plugins since a base commit sits
under HAVE_BLUETOOTH_HID, so that without the define each file is the
base's again; `objcmp` compares the plugin objects of two builds (one
without the define against its base).

Manifest (CSV) columns: rock, status, a b x y l1 r1 l2 r2 menu view home
(what each pad button does), unpadded, notes. rock is a rock's path under
apps/plugins without .rock, or a glob for a family: its exact row wins, else
the longest matching glob. Statuses:
    mapped  its own input code takes pad buttons under HAVE_BLUETOOTH_HID
    shared  reads no input itself; the shared code it links takes them
    pla     reads input only through pluginlib_getaction with pla_main_ctx,
            whose pad entries (A select, B cancel, Options and PS exit) do
    core    reads input only through get_action: Rockbox's own contexts,
            which keymap-pad.c maps; notes say which screens
    none    reads no input at all
    exempt  left without pad buttons; notes say why
unpadded: input-reading sources (paths under apps/plugins, space separated)
deliberately left without pad code, e.g. ones that only wait for any button.
A shared source is declared so in a row of its own: rock = its path under
apps/plugins (lib/display_text.c), status unpadded, notes saying why.

    [rockbox]
    src = "rockbox"                             # the Rockbox source tree
    pad_manifest = "docs/plugin-pad-buttons.csv"
"""

from __future__ import annotations

import csv
import difflib
import fnmatch
import posixpath
import re
import subprocess
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ArbError

PAD = "HAVE_BLUETOOTH_HID"
PLA = "apps/plugins/lib/pluginlib_actions.c"
SRC_EXT = (".c", ".cpp", ".cc", ".S", ".s")
STATUSES = ("mapped", "shared", "pla", "core", "none", "exempt")
BUTTONS = ("a", "b", "x", "y", "l1", "r1", "l2", "r2", "menu", "view", "home")

COMMENT = re.compile(r"/\*.*?\*/|//[^\n]*", re.S)
INCLUDE = re.compile(r'^\s*#\s*include\s*"([^"]+)"', re.M)
# Calls that read buttons; a line declaring one (a type before it) is no call
INPUT = re.compile(r"\b(button_get(?:_w_tmo)?|button_status(?:_wdata)?|get_action|"
                   r"get_custom_action|pluginlib_getaction)\s*\(")
DECL = re.compile(r"^\s*(extern\s+|static\s+|inline\s+|const\s+)*"
                  r"(int|long|unsigned|void|bool|int32_t|uint32_t)\s+\**\w+\s*\(")
DIRECTIVE = re.compile(r"\s*#\s*(if|ifdef|ifndef|elif|else|endif)\b(.*)", re.S)
# The buttons pad code may name: the pad's own and the modifiers. Its d-pad
# is the target's own directions, which need no new code.
BUTTON_NAME = re.compile(r"\bBUTTON_[A-Z0-9_]+")
PAD_NAMES = {f"BUTTON_PAD_{b.upper()}" for b in BUTTONS} | {
    "BUTTON_PAD_ALL", "BUTTON_REL", "BUTTON_REPEAT", "BUTTON_NONE"}

type Row = dict[str, str]


class PadError(ArbError):
    """A build, tree or git history the check cannot read"""


@dataclass
class Report:
    """A check's findings: problems fail it (exit status 1), warnings do not"""

    problems: list[str]
    summary: str
    warnings: list[str] = field(default_factory=list)

    @property
    def status(self) -> int:
        return 1 if self.problems else 0

    def lines(self) -> list[str]:
        return [*self.problems, *(f"warning: {w}" for w in self.warnings), self.summary]


@dataclass
class Rock:
    """One rock: its linker map (build-relative) and its sources with their
    local headers (tree-relative); `facts` fills in the rest"""

    map: str | None
    sources: list[str]
    own: list[str] = field(default_factory=list)            # in this rock alone
    shared: list[str] = field(default_factory=list)
    own_input: list[str] = field(default_factory=list)      # ... that read buttons
    shared_input: list[str] = field(default_factory=list)
    own_pad: list[str] = field(default_factory=list)        # ... that have PAD code
    shared_pad: list[str] = field(default_factory=list)
    calls: dict[str, list[str]] = field(default_factory=dict)   # source: its input calls
    pla_ref: bool = False           # its own code names pla_main_ctx


def strip_comments(text: str) -> str:
    """TEXT without comments, its lines where they were"""
    return COMMENT.sub(lambda m: "\n" * m.group().count("\n"), text)


def input_calls(code: str) -> set[str]:
    """The button-reading functions (comment-free) CODE calls"""
    calls: set[str] = set()
    for line in code.splitlines():
        if not DECL.match(line):
            calls.update(INPUT.findall(line))
    return calls


def rock_map(rock: Path) -> Path | None:
    """The linker map of a rock (the SGT puzzles keep theirs under src/)"""
    m = rock.with_suffix(".map")
    if m.exists():
        return m
    if rock.parent.name == "puzzles" and rock.stem.startswith("sgt-"):
        for sub in ("src", "src/unfinished"):
            m = rock.parent / sub / (rock.stem[4:] + ".map")
            if m.exists():
                return m
    return None


def membership(build: Path, src: Path) -> dict[str, Rock]:
    """Each rock of BUILD (by its path under apps/plugins, no .rock): its map
    and its sources (with local headers) in SRC's apps/plugins"""
    build, src = build.resolve(), src.resolve()
    plugins = build / "apps/plugins"
    if not plugins.is_dir():
        raise PadError(f"{build}: no apps/plugins (an all-plugin build is needed)")
    obj_re = re.compile(re.escape(str(build)) + r"/([^\s()]+?\.(?:o|a))(?:\(([^)]+)\))?")
    by_name: dict[str, list[Path]] = defaultdict(list)      # object basename: its paths
    for o in sorted(build.glob("apps/plugins/**/*.o")):
        by_name[o.name].append(o.relative_to(build))
    sources: dict[str, str | None] = {}
    includes: dict[str, list[str]] = {}

    def source(rel: Path) -> str | None:
        """The source an object was built from"""
        key = str(rel)
        if key not in sources:
            folded = Path(posixpath.normpath("/" + key).lstrip("/"))
            sources[key] = next((str(folded.with_suffix(e)) for e in SRC_EXT
                                 if (src / folded.with_suffix(e)).exists()), None)
        return sources[key]

    def local_includes(f: str) -> list[str]:
        """The files under apps/plugins that F includes with quotes"""
        if f not in includes:
            found = []
            for inc in INCLUDE.findall((src / f).read_text(errors="replace")):
                for base in ((src / f).parent, src / "apps/plugins", src / "apps/plugins/lib"):
                    h = (base / inc).resolve()
                    if h.is_file() and h.is_relative_to(src / "apps/plugins"):
                        found.append(str(h.relative_to(src)))
                        break
            includes[f] = found
        return includes[f]

    def with_headers(files: Iterable[str]) -> set[str]:
        seen: set[str] = set()
        todo = list(files)
        while todo:
            f = todo.pop()
            if f not in seen:
                seen.add(f)
                todo.extend(local_includes(f))
        return seen

    rocks = {}
    for rock in sorted(plugins.rglob("*.rock")):
        name = str(rock.relative_to(plugins).with_suffix(""))
        m = rock_map(rock)
        srcs: set[str] = set()
        if m:
            for path, member in obj_re.findall(m.read_text(errors="replace")):
                if member:          # an archive member: find it beside the archive
                    base = Path(path).parent
                    rels = [p for p in by_name.get(member, []) if base in p.parents][:1]
                else:
                    rels = [Path(path)]
                for rel in rels:
                    s = source(rel)
                    if s and s.startswith("apps/plugins/"):
                        srcs.add(s)
        rocks[name] = Rock(str(m.relative_to(build)) if m else None, sorted(with_headers(srcs)))
    return rocks


def facts(rocks: dict[str, Rock], src: Path) -> dict[str, Rock]:
    """ROCKS with each one's own and shared, input-reading and pad sources"""
    src = src.resolve()
    users = Counter(s for r in rocks.values() for s in r.sources)
    calls: dict[str, set[str]] = {}
    pad: dict[str, bool] = {}
    pla_ref: dict[str, bool] = {}
    for s in users:
        p = src / s
        code = strip_comments(p.read_text(errors="replace")) if p.exists() else ""
        calls[s] = input_calls(code)
        pad[s] = PAD in code
        pla_ref[s] = "pla_main_ctx" in code
    for r in rocks.values():
        r.own = [s for s in r.sources if users[s] == 1]
        r.shared = [s for s in r.sources if users[s] > 1]
        r.own_input = [s for s in r.own if calls[s]]
        r.shared_input = [s for s in r.shared if calls[s]]
        r.own_pad = [s for s in r.own if pad[s]]
        r.shared_pad = [s for s in r.shared if pad[s]]
        r.calls = {s: sorted(calls[s]) for s in r.sources if calls[s]}
        r.pla_ref = any(pla_ref[s] for s in r.own)
    return rocks


def inventory(rocks: dict[str, Rock]) -> list[str]:
    """The rocks (with facts) in short: who reads input, and the shared
    sources that do"""
    own = sum(1 for r in rocks.values() if r.own_input)
    shared = sorted({s for r in rocks.values() for s in r.shared_input})
    lines = [f"{len(rocks)} rocks; {own} read input in their own sources",
             f"{len(shared)} shared sources read input:"]
    lines += [f"  {s} ({sum(1 for r in rocks.values() if s in r.shared)} rocks)" for s in shared]
    nomap = [n for n, r in rocks.items() if r.map is None]
    if nomap:
        lines.append(f"no map: {' '.join(nomap)}")
    return lines


def load_manifest(path: Path) -> list[Row]:
    with path.open(newline="") as f:
        return [{k: (v or "").strip() for k, v in row.items() if k} for row in csv.DictReader(f)]


def source_row(row: Row) -> bool:
    """A row about a shared source rather than a rock"""
    return Path(row["rock"]).suffix in (*SRC_EXT, ".h", ".hpp")


def match(rows: Sequence[Row], names: Iterable[str]) -> tuple[dict[str, Row], list[Row], list[str]]:
    """Each rock's row (its exact one, else the longest glob matching it),
    the rows no rock took, and the clashes"""
    errors: list[str] = []
    exact: dict[str, Row] = {}
    globs: list[Row] = []
    for row in rows:
        if source_row(row):
            continue
        if any(c in row["rock"] for c in "*?["):
            globs.append(row)
        elif row["rock"] in exact:
            errors.append(f"{row['rock']}: two manifest rows")
        else:
            exact[row["rock"]] = row
    found: dict[str, Row] = {}
    used: set[int] = set()
    for name in names:
        row = exact.get(name)
        if row is None:
            hits = sorted((g for g in globs if fnmatch.fnmatchcase(name, g["rock"])),
                          key=lambda g: -len(g["rock"]))
            if len(hits) > 1 and len(hits[0]["rock"]) == len(hits[1]["rock"]):
                errors.append(f"{name}: matched by both {hits[0]['rock']} and {hits[1]['rock']}")
            row = hits[0] if hits else None
        if row is not None:
            found[name] = row
            used.add(id(row))
    return found, [r for r in rows if id(r) not in used], errors


def check(rocks: dict[str, Rock], src: Path, rows: Sequence[Row], only: bool = False) -> Report:
    """Every rock has a manifest row whose claim its sources bear out (ONLY:
    just the rocks the manifest names)"""
    rocks = facts(rocks, src)
    found, unused, errors = match(rows, sorted(rocks))
    errors += [f"{r['rock']}: manifest row matches no built rock"
               for r in unused if not source_row(r)]
    unpadded: dict[str, str] = {}           # source: the row that excuses it
    for row in rows:
        if source_row(row):
            if row.get("status") != "unpadded" or not row.get("notes"):
                errors.append(f"{row['rock']}: a source row needs status unpadded and notes")
            unpadded["apps/plugins/" + row["rock"]] = row["rock"]
        for u in row.get("unpadded", "").split():
            unpadded["apps/plugins/" + u] = row["rock"]
    inputs = {s for r in rocks.values() for s in r.calls}
    padded = {s for r in rocks.values() for s in r.own_pad + r.shared_pad}
    for u, who in sorted(unpadded.items()):
        if u not in inputs:
            errors.append(f"{who}: unpadded {u} is no input source of a rock")
        elif u in padded:
            errors.append(f"{who}: unpadded {u} has {PAD} code")
    counts: Counter[str] = Counter()
    for name, r in sorted(rocks.items()):
        row = found.get(name)
        if row is None:
            if not only:
                errors.append(f"{name}: no manifest row")
            continue
        status = row.get("status", "")
        counts[status] += 1
        errors += [f"{name}: {e}" for e in _rock_problems(r, row, unpadded)]
    summary = (f"{sum(counts.values())} of {len(rocks)} rocks checked, {len(rows)} rows: "
               + ", ".join(f"{counts[s]} {s}" for s in STATUSES) + f"; {len(errors)} problem(s)")
    return Report(errors, summary)


def _rock_problems(r: Rock, row: Row, unpadded: dict[str, str]) -> list[str]:
    """What is wrong with rock R's claim in ROW"""
    st, notes = row.get("status", ""), row.get("notes", "")
    if st not in STATUSES:
        return [f"status {st!r} is not one of {', '.join(STATUSES)}"]
    if r.map is None:
        return ["no linker map to check it against"]
    out = []
    # input sources without pad code that no row excuses
    bare_own = [s for s in r.own_input if s not in r.own_pad and s not in unpadded]
    bare_shared = [s for s in r.shared_input if s not in r.shared_pad and s not in unpadded]
    own_calls: dict[str, list[str]] = defaultdict(list)
    for s in r.own_input:
        if s not in unpadded:
            for c in r.calls[s]:
                own_calls[c].append(s)

    def calls_but(*allowed: str) -> str:
        return "; ".join(f"{c} in {', '.join(fs)}"
                         for c, fs in sorted(own_calls.items()) if c not in allowed)
    if st == "mapped":
        if not r.own_pad:
            out.append(f"mapped, but none of its own sources has {PAD} code")
        if bare_own:
            out.append(f"reads input without pad code in {', '.join(bare_own)}")
    elif st == "shared":
        if r.own_pad:
            out.append(f"has {PAD} code of its own: mapped")
        elif own_calls:
            out.append(f"shared, but reads input itself: {calls_but()}")
        if not any(s in r.shared_pad for s in r.shared_input):
            out.append("shared, but no shared input code it links has pad code")
    elif st == "pla":
        if other := calls_but("pluginlib_getaction"):
            out.append(f"pla, but reads input otherwise: {other}")
        if not r.pla_ref:
            out.append("pla, but its own code never names pla_main_ctx")
        if PLA not in r.shared_pad:
            out.append(f"pla, but it links no {PLA} with pad code")
    elif st == "core":
        if other := calls_but("get_action"):
            out.append(f"core, but reads input otherwise: {other}")
        if not notes:
            out.append("core needs a note naming the screens")
    elif st == "none":
        if r.own_input or r.shared_input:
            out.append(f"none, but reads input in {', '.join(r.own_input + r.shared_input)}")
    elif not notes:
        out.append("exempt needs a reason in notes")
    if st in ("mapped", "shared", "pla", "core") and bare_shared:
        out.append(f"links input code without pad code: {', '.join(bare_shared)}")
    if st in ("mapped", "shared", "pla"):
        missing = [b for b in ("a", "menu") if not row.get(b)]
        if missing:
            out.append(f"{st}, but no meaning given for {', '.join(missing)}")
    return out


def pad_cond(kind: str, cond: str) -> int | None:
    """1 if a branch is taken only with PAD, -1 only without it, 0 if PAD
    plays no part, None if it takes part some other way"""
    if kind in ("ifdef", "ifndef"):
        return (1 if kind == "ifdef" else -1) if cond == PAD else 0
    if PAD not in cond:
        return 0
    c = re.sub(r"\s+", "", cond)
    if re.fullmatch(r"!defined\(?" + PAD + r"\)?", c):
        return -1
    if "||" in c or "!(" in c:
        return None
    uses = re.findall(r"(!?)defined\(?" + PAD + r"\)?", c)
    return 1 if len(uses) == c.count(PAD) and not any(uses) else None


@dataclass
class _Frame:
    """An open conditional"""

    pad: int                # its PAD part: 1, -1 or 0
    pad_only: bool          # the current branch is PAD-only
    keypad: bool            # a CONFIG_KEYPAD conditional


@dataclass
class OffView:
    kept: list[str]                         # the lines a build without PAD sees
    on: list[tuple[int, str, bool]]         # PAD-only lines: number, code, under CONFIG_KEYPAD
    problems: list[str]                     # conditionals too odd to follow


def off_view(text: str) -> OffView:
    """What the compiler sees of TEXT without PAD, as far as conditionals on
    PAD go: the lines left when their PAD-only branches and their directives
    are dropped"""
    lines = text.split("\n")
    code = strip_comments(text).split("\n")
    view = OffView([], [], [])
    stack: list[_Frame] = []
    n = 0
    while n < len(lines):
        start = n
        logical = code[n]
        while lines[n].endswith("\\") and n + 1 < len(lines):
            n += 1
            logical = logical.rstrip("\\") + code[n]
        n += 1
        inside = any(f.pad_only for f in stack)
        keypad = any(f.keypad for f in stack)
        pad_directive = False
        m = DIRECTIVE.match(logical)
        if m:
            kind, cond = m.group(1), m.group(2).strip()
            if kind in ("if", "ifdef", "ifndef"):
                p = pad_cond(kind, cond)
                if p is None:
                    view.problems.append(f"{start + 1}: unclear {PAD} condition: {cond}")
                    p = 0
                stack.append(_Frame(p, p == 1, "CONFIG_KEYPAD" in cond))
                pad_directive = p != 0
            elif not stack:
                view.problems.append(f"{start + 1}: #{kind} without #if")
            elif kind == "elif":
                f = stack[-1]
                if f.pad or PAD in cond:
                    view.problems.append(f"{start + 1}: #elif in or on a {PAD} conditional")
                f.keypad = f.keypad or "CONFIG_KEYPAD" in cond
            elif kind == "else":
                f = stack[-1]
                if f.pad:
                    f.pad_only = f.pad == -1
                    pad_directive = True
            else:
                pad_directive = stack.pop().pad != 0
        if inside or pad_directive:
            if not pad_directive:
                view.on.extend((k + 1, code[k], keypad) for k in range(start, n))
        else:
            view.kept.extend(lines[start:n])
    if stack:
        view.problems.append("unterminated #if")
    return view


def _git(src: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(src), *args], capture_output=True, text=True,
                       errors="replace", check=False)
    if r.returncode:
        raise PadError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


def _at(src: Path, base: str, path: str) -> str | None:
    """PATH as it was in BASE, or None if it was not there"""
    r = subprocess.run(["git", "-C", str(src), "show", f"{base}:{path}"], capture_output=True,
                       text=True, errors="replace", check=False)
    return None if r.returncode else r.stdout


def gate(src: Path, base: str, paths: Sequence[str] = ()) -> Report:
    """Every change to apps/plugins (or PATHS, relative to it unless they
    start with apps/) since BASE sits under PAD: without it each file is
    BASE's again; new pad code names no target buttons and tests no
    CONFIG_KEYPAD"""
    src = src.resolve()
    spec = [p if p.startswith("apps/") else "apps/plugins/" + p for p in paths] or ["apps/plugins"]
    changed = _git(src, "diff", "--name-only", base, "--", *spec).splitlines()
    errors = [f"{f}: new file (pad code goes into the existing ones)"
              for f in _git(src, "ls-files", "--others", "--exclude-standard", "--", *spec)
              .splitlines()]
    warnings = []
    for f in changed:
        p = src / f
        if not p.exists():
            errors.append(f"{f}: deleted")
            continue
        old = _at(src, base, f)
        if old is None:
            errors.append(f"{f}: not in {base}")
            continue
        view = off_view(p.read_text(errors="replace"))
        errors += [f"{f}:{e}" for e in view.problems]

        def norm(ls: Iterable[str]) -> list[str]:
            return [x.rstrip() for x in ls if x.strip()]
        a, b = norm(old.split("\n")), norm(view.kept)
        if a != b:
            diff = list(difflib.unified_diff(a, b, base, f"without {PAD}", n=0, lineterm=""))
            errors.append(f"{f}: without {PAD} it differs from {base}:\n    "
                          + "\n    ".join(diff[2:14]))
        for k, line, _ in view.on:
            bad = sorted(set(BUTTON_NAME.findall(line)) - PAD_NAMES)
            if bad:
                errors.append(f"{f}:{k}: pad code names target buttons: {', '.join(bad)}")
            if "CONFIG_KEYPAD" in line:
                errors.append(f"{f}:{k}: pad code tests CONFIG_KEYPAD")
        under_keypad = [k for k, _, keypad in view.on if keypad]
        if under_keypad:
            warnings.append(f"{f}:{under_keypad[0]}: {len(under_keypad)} line(s) of pad code "
                            "under a CONFIG_KEYPAD conditional (only some keypads get it)")
    return Report(errors, f"{len(changed)} changed file(s) since {base}: "
                          f"{len(errors)} problem(s), {len(warnings)} warning(s)", warnings)


def objcmp(a: Path, b: Path, tool: Callable[[str], str]) -> Report:
    """The plugin objects of builds A and B hold the same code and data (debug
    info aside); TOOL names a binutils tool of the cross toolchain"""
    a, b = a.resolve(), b.resolve()
    objs_a = {o.relative_to(a) for o in a.glob("apps/plugins/**/*.o")}
    objs_b = {o.relative_to(b) for o in b.glob("apps/plugins/**/*.o")}
    problems = [f"only in {'A' if o in objs_a else 'B'}: {o}" for o in sorted(objs_a ^ objs_b)]
    both = sorted(objs_a & objs_b)
    with tempfile.TemporaryDirectory() as tmp:
        stripped = Path(tmp, "o.o")

        def contents(o: Path) -> str:
            try:
                subprocess.run([tool("objcopy"), "--strip-debug", "-R", ".comment",
                                "-R", ".ARM.attributes", str(o), str(stripped)],
                               capture_output=True, text=True, check=True)
                out = subprocess.run([tool("objdump"), "-s", "-r", str(stripped)],
                                     capture_output=True, text=True, check=True).stdout
            except subprocess.CalledProcessError as e:
                raise PadError(f"{o}: {e.stderr.strip()}") from e
            return out.split("\n", 2)[2]        # past the header naming the file
        problems += [f"DIFFERS: {o}" for o in both if contents(a / o) != contents(b / o)]
    return Report(problems, f"{len(both)} plugin objects compared: "
                            f"{'differences above' if problems else 'identical'}")
