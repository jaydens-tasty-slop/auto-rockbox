"""PC samples from the dev shell's profiler (`prof start`, then `prof dump`:
lines of hex PCs) mapped to functions."""

from __future__ import annotations

import bisect
import struct
from collections import Counter

from ...elf import Elf

STT_FUNC = 2
XIP_START = 0xC00000        # PCs from here are code running from flash (XIP)


def functions(elf: Elf) -> list[tuple[int, int, str]]:
    """(start, size, name) of the ELF's functions, sorted; Thumb bit cleared"""
    symtab, strtab = elf.sections.get(".symtab"), elf.sections.get(".strtab")
    if symtab is None or strtab is None:
        raise ValueError(f"{elf.path}: no symbol table")
    strs = elf.raw[strtab.offset:strtab.offset + strtab.size]
    out = []
    for off in range(symtab.offset, symtab.offset + symtab.size, 16):
        name, value, size, info = struct.unpack_from("<IIIB", elf.raw, off)
        if name and info & 0xF == STT_FUNC:
            out.append((value & ~1, size, strs[name:strs.index(b"\0", name)].decode()))
    return sorted(out)


def samples(text: str) -> list[int]:
    """PCs from the dump's lines; other lines (the shell's `ok`, the log
    line counting the samples) are skipped"""
    out = []
    for line in text.splitlines():
        try:
            pcs = [int(w, 16) & ~1 for w in line.split()]
        except ValueError:
            continue
        out += pcs
    return out


def report(pcs: list[int], funcs: list[tuple[int, int, str]], top: int = 30) -> str:
    starts = [f[0] for f in funcs]
    hits: Counter[tuple[str, str]] = Counter()
    for pc in pcs:
        i = bisect.bisect_right(starts, pc) - 1
        name = (funcs[i][2] if i >= 0 and pc < funcs[i][0] + max(funcs[i][1], 1)
                else f"?{pc:#x}")
        hits[(name, "SRAM" if pc < XIP_START else "XIP")] += 1
    lines = [f"{n * 100 / len(pcs):5.1f}%  {n:5d}  {region:4}  {name}"
             for (name, region), n in hits.most_common(top)]
    return "\n".join([*lines, f"{len(pcs)} samples"])
