"""A plugin's FPU use, from the static call graph of its built ELF (Rockbox's
apps/plugins/lib/plugin_fpu.h).

A plugin that switches the FPU on for its main thread must keep its mixer
and timer callbacks and its other threads soft-float: the core saves no FP
state. `check` walks the call graph from the given root functions (those
callbacks and thread entries) and fails on every reachable function with a
VFP instruction; indirect calls it cannot follow are listed, unchecked. It
also fails a function that switches FP access on or off (CPACR) and has VFP
instructions itself: its prologue or epilogue may save or restore FP
registers while access is off (a NOCP UsageFault).

The instructions come from the cross toolchain's objdump ([paths]
toolchain): auto_rockbox.elf reads symbols but decodes no code.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ArbError

FUNC = re.compile(r"^[0-9a-f]+ <([^>]+)>:$")
INSN = re.compile(r"^\s+[0-9a-f]+:\s+(?:[0-9a-f]{4}\s(?:[0-9a-f]{4}\s)?)?\s*(\S+)\s*(.*)$")
TARGET = re.compile(r"<([^>+]+)(?:\+0x[0-9a-f]+)?>")
VFP = re.compile(r"^(v[a-z]+|fmstat|fmxr|fmrx)(\.|$)")
CPACR_FPU = "#15728640"         # 0x00f00000: CP10/CP11 access, as objdump writes an orr/bic of it


class FpuError(ArbError):
    pass


@dataclass
class Function:
    vfp: list[str] = field(default_factory=list)            # its VFP instructions
    calls: dict[str, None] = field(default_factory=dict)    # branch targets, in order
    indirect: list[str] = field(default_factory=list)       # calls through a register
    cpacr: bool = False                                     # switches FP access


def disassemble(elf: Path, objdump: str) -> str:
    r = subprocess.run([objdump, "-d", "--no-show-raw-insn", str(elf)], capture_output=True,
                       text=True, check=False)
    if r.returncode:
        raise FpuError(f"{elf}: {r.stderr.strip()}")
    return r.stdout


def parse(disassembly: str) -> dict[str, Function]:
    """The functions of objdump -d output, by name"""
    funcs: dict[str, Function] = {}
    cur = None
    for line in disassembly.splitlines():
        if m := FUNC.match(line):
            cur = funcs.setdefault(m.group(1), Function())
            continue
        if cur is None or not (m := INSN.match(line)):
            continue
        op, args = m.group(1), m.group(2)
        if VFP.match(op):
            cur.vfp.append(op)
        if op.startswith(("orr", "bic")) and CPACR_FPU in args:
            cur.cpacr = True
        if op.startswith("b") and not op.startswith(("bic", "bfi", "bfc", "bkpt")):
            if t := TARGET.search(args):
                cur.calls[t.group(1)] = None
            elif op.startswith(("blx", "bx")) and "lr" not in args:
                cur.indirect.append(f"{op} {args}".strip())
    return funcs


def vfp_users(funcs: dict[str, Function]) -> list[str]:
    """The functions with VFP instructions, and how many"""
    return [f"{name}: {len(f.vfp)} VFP instructions"
            for name, f in sorted(funcs.items()) if f.vfp]


@dataclass
class Result:
    lines: list[str]
    failures: int

    @property
    def status(self) -> int:
        return 1 if self.failures else 0


def check(funcs: dict[str, Function], roots: list[str]) -> Result:
    """FPU switches in functions with FPU code, then for each root what it
    reaches: FPU code (a failure) and indirect calls (unchecked)"""
    lines = [f"FPU switched in a function with FPU code: {name} ({f.vfp[0]})"
             for name, f in sorted(funcs.items()) if f.cpacr and f.vfp]
    failures = len(lines)
    for root in roots:
        if root not in funcs:
            lines.append(f"{root}: not found")
            failures += 1
            continue
        seen: set[str] = set()
        todo = [root]
        caller: dict[str, str | None] = {root: None}     # how the walk first got to each
        while todo:
            name = todo.pop()
            if name in seen or name not in funcs:
                continue
            seen.add(name)
            f = funcs[name]
            chain = []
            n: str | None = name
            while n:
                chain.append(n)
                n = caller[n]
            via = " <- ".join(chain)
            if f.vfp:
                lines.append(f"FPU in interrupt path: {via} ({f.vfp[0]})")
                failures += 1
            lines += [f"indirect call (unchecked): {via}: {ind}" for ind in f.indirect]
            for c in f.calls:
                caller.setdefault(c, name)
                todo.append(c)
        lines.append(f"{root}: {len(seen)} functions checked")
    return Result(lines, failures)
