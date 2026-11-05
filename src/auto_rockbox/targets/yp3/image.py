"""A flash image for a run: the ELF placed into a full image of this player's
flash, plus the areas a YP3_DEV build reads at boot.

The base image is a read-back of the user's own player: its bootloader
[0, 0xE000) and settings [0x1FC000, 2 MiB) are kept byte for byte (and never
written anyway). The stock bootloader's partition table (0xD000, left as it
is) points its FIRM entry at 0xE000, and the bootloader copies
[EARLY_COPY, +EARLY_MAX) to SRAM 0x804C00 and calls it there: Rockbox's early
code, which arms the recovery watchdog and sets up XIP before jumping to
flash. The bootloader does not check the FIRM CRC fields, so they are not
updated and the partition table never needs a rewrite.

A YP3_DEV build reads its run config (one sector of `key=value` lines) and
the sdpush bundle (files it copies to the card when they differ) through XIP
(apps/dev_shell.c, dev-yp3.c).
"""

from __future__ import annotations

import binascii
import struct
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from ...elf import Elf
from ...errors import ArbError
from .rom import IMAGE_SIZE, SECTOR, WRITABLE

XIP_ALIAS = 0xC00000        # flash offset 0 in the CPU's address space
FIRM_END = 0xE000 + 0x192B18    # end of the partition table's FIRM span
CODE_MIN = 0x10000          # loaded sections start at or above this flash offset
EARLY_COPY = 0xE430         # the bootloader copies [0xE430, 0xF430) to SRAM 0x804C00
EARLY_MAX = 0x1000
EARLY_END = "__yp3_early_code_end"  # .text up to this symbol is the early code
LOADED = (".text", ".vectors", ".data")
DEV_MARK = "yp3_dev_return_seconds"     # only YP3_DEV builds return to ROM USB by themselves

XIP_CODEC_AREA = 0x0E0000   # XIP_CODEC_AREA_START (yp3.h) - XIP_ALIAS
XIP_CODECS = "lib/rbcodec/codecs/xip/codecs.flash"  # its image, in the ELF's build dir
DEV_BUNDLE = 0x150000       # YP3_DEV_BUNDLE_OFFSET: the sdpush bundle, up to DEV_CONFIG
DEV_CONFIG = 0x1F3000       # YP3_DEV_CONFIG_OFFSET: one sector of "key=value\n" lines
BUNDLE_MAGIC = 0x48535059   # "YPSH": starts each sdpush entry


class ImageError(ArbError, ValueError):
    """The ELF, the base image or the run's extras do not make a valid image"""


def _pad4(b: bytes) -> bytes:
    return b + bytes(-len(b) % 4)


def place_elf(elf: Elf, base: bytes) -> tuple[bytearray, int]:
    """BASE with ELF's loaded sections at their load addresses, and its early
    code copied where the bootloader takes it from; also the end offset"""
    if len(base) != IMAGE_SIZE:
        raise ImageError(f"the base image is {len(base)} bytes, not {IMAGE_SIZE}")
    img = bytearray(base)
    missing = [n for n in LOADED if n not in elf.sections]
    if missing:
        raise ImageError(f"{elf.path}: no section {', '.join(missing)}")
    end = 0
    for name in LOADED:
        sec = elf.sections[name]
        start = _load_address(elf, sec.addr) - XIP_ALIAS
        if start < CODE_MIN or start + sec.size > FIRM_END:
            raise ImageError(f"{name} [{start:#x}+{sec.size:#x}] outside the FIRM span")
        img[start:start + sec.size] = elf.raw[sec.offset:sec.offset + sec.size]
        end = max(end, start + sec.size)
    text = elf.sections[".text"]
    if EARLY_END not in elf.symbols:
        raise ImageError(f"{elf.path}: no symbol {EARLY_END}")
    copy = elf.symbols[EARLY_END][0] - text.addr
    if not 0 < copy <= EARLY_MAX:
        raise ImageError(f"early code size {copy:#x} out of range")
    start = _load_address(elf, text.addr) - XIP_ALIAS
    img[EARLY_COPY:EARLY_COPY + copy] = img[start:start + copy]
    return img, end


def _load_address(elf: Elf, vaddr: int) -> int:
    for s in elf.segments:
        if s.vaddr <= vaddr < s.vaddr + s.memsz:
            return s.paddr + vaddr - s.vaddr
    raise ImageError(f"{elf.path}: no load segment holds {vaddr:#x}")


def dev_config(settings: Mapping[str, str]) -> bytes:
    """The run config sector's text: one `key=value` line each; the firmware
    takes the first line of a key"""
    for k, v in settings.items():
        if not k or "=" in k or "\n" in k + v:
            raise ImageError(f"run setting {k}={v}: a key without '=' and no newlines")
    text = "".join(f"{k}={v}\n" for k, v in settings.items()).encode()
    if len(text) > SECTOR:
        raise ImageError(f"run config of {len(text)} bytes over one sector")
    return text


def sdpush_bundle(files: Iterable[tuple[str, bytes]]) -> bytes:
    """Entries {magic, path length, data length, CRC-32 of data}, then the card
    path and the data, each padded to 4 bytes; erased flash (0xFF) ends it"""
    bundle = b""
    for sdpath, data in files:
        if not sdpath.startswith("/"):
            raise ImageError(f"{sdpath}: card paths start with /")
        name = sdpath.encode()
        bundle += struct.pack("<4I", BUNDLE_MAGIC, len(name), len(data),
                              binascii.crc32(data) & 0xFFFFFFFF) + _pad4(name) + _pad4(data)
    if len(bundle) > DEV_CONFIG - DEV_BUNDLE:
        raise ImageError(f"sdpush bundle too large ({len(bundle)} bytes, "
                         f"{DEV_CONFIG - DEV_BUNDLE} fit; copy big files over USB storage)")
    return bundle


def dev_image(elf_path: Path, base: bytes, config: Mapping[str, str], *,
              pushes: Sequence[tuple[Path, str]] = (), blobs: Sequence[tuple[Path, int]] = (),
              release: bool = False) -> bytes:
    """The whole image for ELF_PATH: CONFIG as the run config, PUSHES (local
    file, card path) in the sdpush bundle, BLOBS (file, flash offset) and
    the build's XIP codecs written in. RELEASE allows an ELF that is not a
    YP3_DEV build (it never returns to ROM USB by itself)."""
    elf = Elf(elf_path)
    if DEV_MARK not in elf.symbols and not release:
        raise ImageError(f"{elf_path}: not a YP3_DEV build, so it never returns to ROM USB "
                         "on its own; rebuild it as a dev build or pass --release")
    img, end = place_elf(elf, base)
    if end > DEV_BUNDLE:
        raise ImageError(f"the image ends at {end:#x}, past the dev areas at {DEV_BUNDLE:#x}")
    bundle = sdpush_bundle((sd, local.read_bytes()) for local, sd in pushes)
    text = dev_config(config)
    img[DEV_BUNDLE:DEV_CONFIG] = bundle + b"\xff" * (DEV_CONFIG - DEV_BUNDLE - len(bundle))
    img[DEV_CONFIG:DEV_CONFIG + SECTOR] = text + b"\xff" * (SECTOR - len(text))
    xip = elf_path.parent / XIP_CODECS
    extra = [(xip, XIP_CODEC_AREA)] if xip.exists() else []
    for f, off in [*extra, *blobs]:
        data = f.read_bytes()
        if not (WRITABLE[0] <= off and off + len(data) <= DEV_BUNDLE):
            raise ImageError(f"{f}@{off:#x}: outside [{WRITABLE[0]:#x}, {DEV_BUNDLE:#x})")
        img[off:off + len(data)] = data
    return bytes(img)


def parse_push(spec: str) -> tuple[Path, str]:
    """LOCAL=/card/path"""
    local, sep, sdpath = spec.partition("=")
    if not sep or not sdpath.startswith("/"):
        raise ImageError(f"--push {spec}: LOCAL=/card/path needed")
    return Path(local), sdpath


def parse_blob(spec: str) -> tuple[Path, int]:
    """FILE@OFFSET"""
    f, sep, off = spec.rpartition("@")
    try:
        if sep:
            return Path(f), int(off, 0)
    except ValueError:
        pass
    raise ImageError(f"--blob {spec}: FILE@OFFSET needed")
