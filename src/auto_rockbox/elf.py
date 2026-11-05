"""Just enough ELF32 (little-endian, ARM) to find a firmware image's sections,
symbols and load segments without the toolchain, and addr2line through it
for names of addresses."""

from __future__ import annotations

import struct
import subprocess
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

PT_LOAD = 1
SHT_SYMTAB = 2
STT_FUNC = 2


@dataclass(frozen=True)
class Segment:
    vaddr: int
    paddr: int
    data: bytes             # file bytes only (memsz beyond is zero-filled bss)
    memsz: int


@dataclass(frozen=True)
class Section:
    name: str
    addr: int
    size: int
    offset: int
    type: int


class Elf:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.raw = path.read_bytes()
        if self.raw[:4] != b"\x7fELF" or self.raw[4] != 1 or self.raw[5] != 1:
            raise ValueError(f"{path}: not a little-endian ELF32 file")
        (self.entry, self.phoff, self.shoff, _flags, _ehsize, self.phentsize, self.phnum,
         self.shentsize, self.shnum, self.shstrndx) = struct.unpack_from(
            "<IIIIHHHHHH", self.raw, 24)

    @cached_property
    def segments(self) -> list[Segment]:
        out = []
        for i in range(self.phnum):
            (ptype, off, vaddr, paddr, filesz, memsz, _fl, _al) = struct.unpack_from(
                "<8I", self.raw, self.phoff + i * self.phentsize)
            if ptype == PT_LOAD and memsz:
                out.append(Segment(vaddr, paddr, self.raw[off:off + filesz], memsz))
        return out

    @cached_property
    def _shdrs(self) -> list[tuple[int, ...]]:
        """name, type, flags, addr, offset, size, link, info, align, entsize"""
        return [struct.unpack_from("<10I", self.raw, self.shoff + i * self.shentsize)
                for i in range(self.shnum)]

    def _strings(self, index: int) -> bytes:
        h = self._shdrs[index]
        return self.raw[h[4]:h[4] + h[5]]

    @cached_property
    def sections(self) -> dict[str, Section]:
        names = self._strings(self.shstrndx)
        return {(n := _cstr(names, h[0])): Section(n, h[3], h[5], h[4], h[1])
                for h in self._shdrs}

    @cached_property
    def symbols(self) -> dict[str, tuple[int, int]]:
        """name -> (value, size)"""
        out: dict[str, tuple[int, int]] = {}
        for h in self._shdrs:
            if h[1] != SHT_SYMTAB:
                continue
            strs = self._strings(h[6])
            for off in range(h[4], h[4] + h[5], 16):
                name_off, value, size = struct.unpack_from("<III", self.raw, off)
                if name_off:
                    out[_cstr(strs, name_off)] = (value, size)
        return out

    @cached_property
    def functions(self) -> list[tuple[int, int, str]]:
        """(start, size, name) of every function symbol, sorted by start, with
        the Thumb bit cleared"""
        out = []
        for h in self._shdrs:
            if h[1] != SHT_SYMTAB:
                continue
            strs = self._strings(h[6])
            for off in range(h[4], h[4] + h[5], 16):
                name_off, value, size, info = struct.unpack_from("<IIIB", self.raw, off)
                if name_off and info & 0xF == STT_FUNC:
                    out.append((value & ~1, size, _cstr(strs, name_off)))
        return sorted(out)

    def address(self, name: str) -> int:
        """A section's or else a symbol's address"""
        if name in self.sections:
            return self.sections[name].addr
        if name in self.symbols:
            return self.symbols[name][0]
        raise KeyError(f"{self.path.name}: no section or symbol {name}")


def _cstr(table: bytes, off: int) -> str:
    return table[off:table.index(b"\0", off)].decode()


class Symbolizer:
    """Function and line for code addresses, by the toolchain's addr2line"""

    def __init__(self, elf: Path, addr2line: str) -> None:
        self.elf = elf
        self.addr2line = addr2line

    def names(self, addrs: list[int]) -> list[str]:
        if not addrs:
            return []
        try:
            out = subprocess.run([self.addr2line, "-f", "-C", "-s", "-e", str(self.elf)]
                                 + [f"{a:#x}" for a in addrs],
                                 capture_output=True, text=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError):
            return ["?"] * len(addrs)
        lines = out.splitlines()
        return [f"{lines[2 * i]} {lines[2 * i + 1]}" for i in range(len(addrs))]
