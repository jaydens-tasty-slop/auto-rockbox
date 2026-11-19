"""Does a port's tree change other targets' builds? `arb tree build` builds
one target from a Rockbox tree; `arb tree compare` compares two such builds
(the port's base and its branch) output by output.

Every build runs in one work directory from one short relative source path,
a link switched between the trees: the paths compiled into the objects
(__FILE__ in asserts and panics) match, and the one shell command that
writes make.dep stays under Linux's 128 KiB limit for an argument (an
absolute path and an all-plugin target overflow it). `make -k`: a plugin
that fails in both builds is a fact about the base, not the branch; the
failures are compared too, and the objects of a target whose core doesn't
link (an upstream linker script with this gcc) still compare.

Each output that differs gets the first class that explains it:
- the same code and data: sections and relocations equal once debug info
  (it carries header line numbers) and compiler-numbered local names
  (CSWTCH.25 for .26, one function more in the file) are set aside
- the module build ID only: a hash of the module API headers' text, so any
  change there, gated or not, changes it in every module (and in the core)
- a build timestamp (__DATE__, __TIME__)
- else it DIFFERS.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ArbError

OUTPUTS = ("*.o", "*.rock", "*.codec", "*.ovl", "rockbox.elf")
FAILED = "failed.txt"
ID_FILE = "module_build_id.lds"
VERSION = "arb"                 # the version string, the same in both builds
MODULE_ID = re.compile(r"module_build_id = 0x([0-9a-f]+);")
# make: *** [target] Error 1, or [makefile:line: target]
MAKE_ERROR = re.compile(r"\*\*\* \[(?:[^\]]*\s)?([^\]\s]+)\] Error \d+")
TIMESTAMP = re.compile(rb"\d\d:\d\d:\d\d"
                       rb"|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) [ \d]\d \d{4}")
NUMBERED = re.compile(r"\b([A-Za-z_][\w$]*)\.\d+\b")
ELF_MAGIC = b"\x7fELF"

SAME = "identical"
CODE = "same code and data (debug info, numbered names aside)"
MODULE = "module build ID only"
STAMP = "build timestamp"
DIFFERS = "DIFFERS"


class TreeError(ArbError):
    pass


def build(src: Path, out: Path, work: Path, *, target: str, configure: str, prefix: str,
          jobs: int, note: Callable[[str], None]) -> list[str]:
    """Builds TARGET from the Rockbox tree SRC in WORK (configure's options
    plus CONFIGURE; PREFIX: the cross compiler's) and copies its outputs, the
    failures and the module build ID to OUT, a new directory. Returns the
    failures (make targets, relative to WORK)."""
    if out.exists() and any(out.iterdir()):
        raise TreeError(f"{out}: not empty (a new directory for the outputs)")
    if not (src / "tools" / "configure").is_file():
        raise TreeError(f"{src}: not a Rockbox tree (no tools/configure)")
    work.mkdir(parents=True, exist_ok=True)
    link = work.parent / f"{work.name}-src"
    if link.exists() and not link.is_symlink():
        raise TreeError(f"{link}: in the way (the source link's place)")
    link.unlink(missing_ok=True)
    link.symlink_to(src.resolve())
    if (work / "Makefile").exists():
        subprocess.run(["make", "clean"], cwd=work, capture_output=True, check=False)
    with (work / "configure.log").open("w") as log:
        r = subprocess.run([f"../{link.name}/tools/configure", "--no-ccache",
                            f"--compiler-prefix={prefix}", f"--target={target}", "--type=N",
                            *shlex.split(configure)],
                           cwd=work, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                           check=False)
    if r.returncode or not (work / "Makefile").exists():
        raise TreeError(f"configure failed: {work / 'configure.log'}")
    makefile = work / "Makefile"
    text, n = re.subn(r"(?m)^export ROOTDIR=.*$", f"export ROOTDIR=../{link.name}",
                      makefile.read_text())
    if n != 1:
        raise TreeError(f"{makefile}: no ROOTDIR line to point at the link")
    makefile.write_text(text)
    (work / "make.dep").unlink(missing_ok=True)
    note(f"building {target} from {src} in {work}")
    with (work / "make.log").open("w") as log:
        subprocess.run(["nice", "-n19", "make", "-k", f"-j{jobs}"], cwd=work, stdout=log,
                       stderr=subprocess.STDOUT, env={**os.environ, "VERSION": VERSION},
                       check=False)
    failed = failures((work / "make.log").read_text(errors="replace"), work)
    out.mkdir(parents=True, exist_ok=True)
    for pattern in OUTPUTS:
        for f in work.rglob(pattern):
            dest = out / f.relative_to(work)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
    (out / FAILED).write_text("".join(f"{f}\n" for f in failed))
    if (work / ID_FILE).exists():
        shutil.copy2(work / ID_FILE, out / ID_FILE)
    return failed


def failures(make_log: str, work: Path) -> list[str]:
    """The make targets that failed, relative to WORK"""
    prefix = f"{work.resolve()}/"
    return sorted({t.removeprefix(prefix) for t in MAKE_ERROR.findall(make_log)})


def module_id(build: Path) -> bytes | None:
    """The build's module build ID as it sits in memory (little-endian)"""
    f = build / ID_FILE
    m = MODULE_ID.search(f.read_text()) if f.exists() else None
    return int(m.group(1), 16).to_bytes(4, "little") if m else None


def _stamps(data: bytes) -> bytes:
    return TIMESTAMP.sub(lambda m: b"#" * len(m.group(0)), data)


def classify(a: bytes, b: bytes, ids: tuple[bytes, bytes] | None,
             elf_dump: Callable[[bytes], str]) -> str:
    """The class of output A's difference from B (module IDs: IDS, A's and
    B's); ELF_DUMP gives an ELF file's sections and relocations as text,
    debug info left out"""
    if a == b:
        return SAME
    elf = a.startswith(ELF_MAGIC) and b.startswith(ELF_MAGIC)

    def same(x: bytes, y: bytes) -> bool:
        if not elf:
            return x == y
        return NUMBERED.sub(r"\1.N", elf_dump(x)) == NUMBERED.sub(r"\1.N", elf_dump(y))

    if elf and same(a, b):
        return CODE
    if ids and ids[0] != ids[1]:
        a = a.replace(ids[0], ids[1])
        if same(a, b):
            return MODULE
    if same(_stamps(a), _stamps(b)):
        return STAMP
    return DIFFERS


@dataclass
class Comparison:
    classes: dict[str, list[str]] = field(default_factory=dict)
    only_a: list[str] = field(default_factory=list)
    only_b: list[str] = field(default_factory=list)
    failed_a: list[str] = field(default_factory=list)
    failed_b: list[str] = field(default_factory=list)

    @property
    def status(self) -> int:
        return 1 if (self.classes.get(DIFFERS) or self.only_a or self.only_b
                     or self.failed_a or self.failed_b) else 0

    def lines(self) -> list[str]:
        out = [f"DIFFERS: {f}" for f in self.classes.get(DIFFERS, [])]
        out += [f"only in A: {f}" for f in self.only_a]
        out += [f"only in B: {f}" for f in self.only_b]
        out += [f"failed in A only: {f}" for f in self.failed_a]
        out += [f"failed in B only: {f}" for f in self.failed_b]
        for c in (STAMP, CODE):
            out += [f"{c}: {f}" for f in self.classes.get(c, [])]
        counts = ", ".join(f"{len(self.classes[c])} {c}"
                           for c in (SAME, CODE, MODULE, STAMP, DIFFERS) if self.classes.get(c))
        out.append(f"{sum(len(v) for v in self.classes.values())} outputs: {counts or 'none'}")
        return out


def _outputs(build: Path) -> set[str]:
    return {str(f.relative_to(build)) for p in OUTPUTS for f in build.rglob(p)}


def _failed(build: Path) -> list[str]:
    f = build / FAILED
    return f.read_text().split() if f.exists() else []


def compare(a: Path, b: Path, elf_dump: Callable[[bytes], str]) -> Comparison:
    """Builds A and B (`build`'s outputs) output by output"""
    outs_a, outs_b = _outputs(a), _outputs(b)
    if not outs_a or not outs_b:
        raise TreeError(f"no outputs in {a if not outs_a else b}: a `tree build` directory?")
    ids_a, ids_b = module_id(a), module_id(b)
    ids = (ids_a, ids_b) if ids_a and ids_b else None
    c = Comparison(only_a=sorted(outs_a - outs_b), only_b=sorted(outs_b - outs_a))
    fa, fb = set(_failed(a)), set(_failed(b))
    c.failed_a, c.failed_b = sorted(fa - fb), sorted(fb - fa)
    for f in sorted(outs_a & outs_b):
        cls = classify((a / f).read_bytes(), (b / f).read_bytes(), ids, elf_dump)
        c.classes.setdefault(cls, []).append(f)
    return c


def elf_dumper(objcopy: str, objdump: str) -> Callable[[bytes], str]:
    """An ELF file's sections and relocations as text, without its debug
    info, its compiler's note or its attributes (the cross binutils)"""
    def dump(data: bytes) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            raw, stripped = Path(tmp, "a.o"), Path(tmp, "b.o")
            raw.write_bytes(data)
            try:
                subprocess.run([objcopy, "--strip-debug", "-R", ".comment",
                                "-R", ".ARM.attributes", str(raw), str(stripped)],
                               capture_output=True, text=True, check=True)
                text = subprocess.run([objdump, "-s", "-r", str(stripped)],
                                      capture_output=True, text=True, check=True).stdout
            except subprocess.CalledProcessError as e:
                raise TreeError(f"{e.cmd[0]}: {e.stderr.strip()}") from e
        return "\n".join(line for line in text.splitlines() if "file format" not in line)
    return dump
