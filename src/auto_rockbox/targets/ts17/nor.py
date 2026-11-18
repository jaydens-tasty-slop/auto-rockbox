"""The TS17's 4 MiB SPI NOR from download mode, through the ops gadget.

Download mode leaves the NOR/cache block unclocked and in reset; `setup`
brings it up as the boot ROM's first stage (mbrec) does. Reads go through the
XIP cache's remap entry 2 (window 0x12000000) in 32 KiB steps, descrambled
unless `raw`. Writes call the boot ROM's own SPI-NOR routines (CALL ops),
which store bytes as given: an image for XIP is encoded first (rdm.py).
Calling ROM code is fine for tooling; Rockbox's flash driver is its own.

Flash map: the boot chain (mbrec A/B, partition tables, stock's recovery
image fw0_rec) below 0x14000, which writes refuse unless asked; stock's
application fw0_sys at 0x14000, which our image replaces (stock comes back by
restoring it from a dump of the stored cells, `nor dump --raw`). Our image's
first sector is Rockbox's rescue stub (rescue-lark.S), written once: changing
it, or putting one there, takes `rescue`.
"""

from __future__ import annotations

import hashlib
import io
import struct
from collections.abc import Callable
from typing import BinaryIO

from . import rdm
from .adfu import Ts17Error
from .gadget import Ops, Session

FLASH_SIZE = 0x400000
SECTOR = 0x1000                 # erase unit
FW0_SYS = 0x14000               # stock's application: our image runs from here
FW0_SYS_SIZE = 0x1A7000
BOOT_END = FW0_SYS              # mbrec A/B, the partition tables and fw0_rec end here
XIP_BASE = 0x10000000           # remap entry 0's virtual base, where fw0_sys runs
RESCUE = FW0_SYS                # the rescue stub's sector, ahead of Rockbox
RESCUE_MARK = b"RBRS"           # at +8 in the stub
# The stub is frozen for good (the user, 2026-11-14: never to be touched
# again): v2, its sector as the CPU reads it. rockslop carries the same
# bytes, and ts17dev/build.sh fails a build whose stub differs.
RESCUE_SHA256 = "aa13ea64f60a71227ae55318f91dac94d0f0e7220b6bf73f98b6067367c44f0f"
APP = FW0_SYS + SECTOR          # Rockbox, after the stub
APP_BASE = XIP_BASE + SECTOR    # Rockbox's vector table, after the stub
RAM0_BASE, RAM0_SIZE = 0x01000000, 0x24000  # Rockbox's initial stack pointer is in here

NOR_CLK_DIV = 0x40001020        # NOR clock divider
NOR_CLK_DIV_192_3 = 0x102       # 192 MHz PLL / 3, as mbrec sets it
CLK_EN = 0x40001004             # module clock enables
RST_CTL = 0x40000000            # module resets (1 = running)
NOR_MODULE = 1 << 8             # the NOR/cache block's bit in both
NOR_CTL = 0x40028000            # SPI0, the NOR controller
NOR_CTL_MBREC_SET = 0x00300000  # bits mbrec sets
NOR_CTL_MBREC_CLEAR = 0x100     # bit mbrec clears
NOR_CTL_RANDOMIZER = 0x20001000  # descramble reads (XIP + "rdm en")
CACHE_CTL = 0x40014000          # XIP cache; 0x21 enables (ignored until clocked)
CACHE_ON = 0x21
CACHE_INV = 0x40014004          # write 1 to drop every line; bit 0 reads 1 until done
REMAP0 = 0x40010300             # remap entry n at +8n: [+0] virtual base | ON, [+4] NOR offset
REMAP2 = 0x40010310             # entry 2: the tools' read window
REMAP_ON = 0xF
REMAP_CRYPT = 1 << 31           # in the offset: mbrec's second attempt
WINDOW = 0x12000000             # entry 2's virtual base, as mbrec uses it
# The version block's model text (NOR 0x1338, in the boot area no write
# reaches without --boot): per model, so the tools can tell two Lark players
# apart before a write, as both enumerate as 10d6:10d6 (Lark fact L-F-0018)
MODEL_TAG_SECTOR = 0x1000
MODEL_TAG_AT, MODEL_TAG_LEN = 0x338, 32     # within that sector
COPY_BUF = 0x01048000           # 32 KiB RAM for reads and writes, after the gadget's output
CHUNK = 0x8000
SPIN_10US = 2000                # DELAY spins: generous at any likely clock

# The boot ROM's SPI-NOR API (its table at ROM 0x5d10). info = 0 picks the
# ROM's default: SPI0, one data line.
ROM_NOR_READ = 0x4219           # (info, addr, buf, len)
ROM_NOR_WRITE = 0x42DD          # (info, addr, buf, len) -> 0 on success
ROM_NOR_ERASE = 0x4135          # (info, addr, size), 4 KiB units -> 0 on success

Progress = Callable[[str], None]


def pad(data: bytes, unit: int) -> bytes:
    """DATA filled with erased-flash bytes up to a multiple of UNIT"""
    return data + b"\xff" * (-len(data) % unit)


def invalidate(ops: Ops) -> Ops:
    return ops.w32(CACHE_INV, 1).poll(CACHE_INV, 1, 0)


def setup(raw: bool = False) -> Ops:
    """mbrec's one-time NOR and cache bring-up; RAW leaves the randomizer off
    so reads return the stored cells"""
    ops = Ops().w32(NOR_CLK_DIV, NOR_CLK_DIV_192_3).w32(CACHE_CTL, 0)
    ops.rmw(CLK_EN, 0, NOR_MODULE).delay(SPIN_10US)
    ops.rmw(RST_CTL, NOR_MODULE, 0).delay(SPIN_10US // 10)
    ops.rmw(RST_CTL, 0, NOR_MODULE).delay(SPIN_10US)
    invalidate(ops.w32(CACHE_CTL, CACHE_ON))
    ops.rmw(NOR_CTL, 0, NOR_CTL_MBREC_SET)
    ops.rmw(NOR_CTL, NOR_CTL_RANDOMIZER, 0 if raw else NOR_CTL_RANDOMIZER)
    return ops.rmw(NOR_CTL, NOR_CTL_MBREC_CLEAR, 0)


def map_window(ops: Ops, offset: int, crypt: bool = False) -> Ops:
    """Remap entry 2 onto NOR OFFSET: WINDOW then reads it"""
    invalidate(ops.w32(REMAP2, 0).w32(REMAP2 + 4, 0))
    ops.w32(REMAP2, WINDOW | REMAP_ON).w32(REMAP2 + 4, offset | (REMAP_CRYPT if crypt else 0))
    return invalidate(ops)


def launch_ops(slot: int, reset: int, token: int) -> Ops:
    """Runs the image at NOR SLOT in place: remap entry 0 (XIP_BASE) onto it,
    then its reset vector with TOKEN in r0. It never returns to the ROM."""
    ops = setup()
    invalidate(ops.w32(REMAP0, 0).w32(REMAP0 + 4, 0))
    invalidate(ops.w32(REMAP0, XIP_BASE | REMAP_ON).w32(REMAP0 + 4, slot))
    return ops.call(reset & ~1, token)


def read(s: Session, start: int, length: int, out: BinaryIO, *, raw: bool = False,
         crypt: bool = False, progress: Progress | None = None) -> None:
    """LENGTH bytes from NOR START into OUT, as the CPU reads them (RAW: the
    stored cells)"""
    s.values(setup(raw))
    for off in range(start, start + length, CHUNK):
        s.values(map_window(Ops(), off, crypt).copy(WINDOW, COPY_BUF, CHUNK))
        out.write(s.link.read(COPY_BUF, min(CHUNK, start + length - off)))
        if progress:
            progress(f"{(off + CHUNK - start) // 1024} KiB")


def model_tag(s: Session) -> str:
    """The model text of the player on the link"""
    buf = io.BytesIO()
    read(s, MODEL_TAG_SECTOR, SECTOR, buf)
    raw = buf.getvalue()[MODEL_TAG_AT:MODEL_TAG_AT + MODEL_TAG_LEN]
    return raw.split(b"\0", 1)[0].decode("ascii", "replace")


def _rom_read(s: Session, addr: int, n: int) -> bytes:
    s.values(Ops().call(ROM_NOR_READ, 0, addr, COPY_BUF, n))
    return s.link.read(COPY_BUF, n)


def has_rescue(plain: bytes) -> bool:
    """PLAIN (an image as the CPU reads it) starts with a rescue stub"""
    return plain[8:12] == RESCUE_MARK


def is_frozen_rescue(plain: bytes) -> bool:
    """PLAIN's first sector is the frozen rescue stub"""
    return hashlib.sha256(plain[:SECTOR]).hexdigest() == RESCUE_SHA256


def _not_elf(plain: bytes, name: str) -> None:
    if plain[:4] == b"\x7fELF":
        raise Ts17Error(f"{name} is an ELF file: flash its `objcopy -O binary` image")


def check_app(plain: bytes, name: str) -> int:
    """Raises unless PLAIN, Rockbox for APP behind the stub, has vectors the
    stub accepts (else it sends every boot to download mode, or powers off
    on battery); returns its reset vector"""
    _not_elf(plain, name)
    if len(plain) < 8:
        raise Ts17Error(f"{name}: no vector table")
    sp, entry = struct.unpack_from("<II", plain)
    if ((sp - RAM0_BASE - 1) & 0xFFFFFFFF >= RAM0_SIZE or not entry & 1
            or (entry - APP_BASE) & 0xFFFFFFFF >= FW0_SYS_SIZE - SECTOR):
        raise Ts17Error(f"{name}: the stub would refuse Rockbox's vectors "
                        f"(SP {sp:08x}, reset {entry:08x})")
    return entry


def check_image(plain: bytes, name: str) -> int:
    """Raises unless PLAIN is an image the boot chain can start from
    fw0_sys; returns its reset vector. With the rescue stub, Rockbox's
    vectors after it must pass the stub's own check."""
    _not_elf(plain, name)
    (reset,) = struct.unpack_from("<I", plain, 4)
    if not has_rescue(plain):
        if not XIP_BASE <= reset - 1 < XIP_BASE + len(plain):
            raise Ts17Error(f"{name}: reset vector {reset:08x} is not inside the image "
                            f"at {XIP_BASE:08x}")
        return reset
    if not (reset & 1 and XIP_BASE < reset < APP_BASE):
        raise Ts17Error(f"{name}: the rescue stub's reset vector {reset:08x} is not in it")
    if len(plain) < SECTOR + 8:
        raise Ts17Error(f"{name}: nothing after the rescue stub")
    check_app(plain[SECTOR:], name)
    return reset


def _check_rescue(s: Session, want: bytes) -> None:
    """Refuses to change the rescue stub's sector to WANT (cells) while a
    stub is stored there or WANT holds one"""
    have = _rom_read(s, RESCUE, SECTOR)
    if have != want and (has_rescue(rdm.decode(have)) or has_rescue(rdm.decode(want))):
        raise Ts17Error(f"this would change the rescue stub at {RESCUE:#x}: install it "
                        "where there is none with `nor program --rescue`")


def _check_stub_behind(s: Session) -> None:
    """Refuses Rockbox on its own unless the stub stored at RESCUE is there
    to start it (stock's start would jump into what was just written)"""
    if not has_rescue(rdm.decode(_rom_read(s, RESCUE, SECTOR))):
        raise Ts17Error(f"no rescue stub at {RESCUE:#x} to start Rockbox: write a whole "
                        "image there first (`nor program --rescue IMAGE 0x14000`)")


def write_cells(s: Session, start: int, cells: bytes, *, boot: bool = False,
                rescue: bool = False, progress: Progress | None = None) -> int:
    """Makes flash at START match CELLS: per sector, read what is stored and
    if it differs erase, program and verify. Identical sectors are left
    alone to spare wear. Returns how many were rewritten. The boot chain
    takes BOOT, installing the stub where there is none (or removing it,
    stock's own start going back) RESCUE. A stub is only ever the frozen
    one, and Rockbox written on its own needs one stored ahead of it."""
    if start % SECTOR or len(cells) % SECTOR or start + len(cells) > FLASH_SIZE:
        raise Ts17Error("a flash write must be whole 4 KiB sectors inside the flash")
    if start < BOOT_END and not boot:
        raise Ts17Error(f"the range touches the boot chain (below {BOOT_END:#x})")
    stub = None
    if start <= RESCUE < start + len(cells):
        stub = cells[RESCUE - start:RESCUE - start + SECTOR]
        plain = rdm.decode(stub)
        if has_rescue(plain) and not is_frozen_rescue(plain):
            raise Ts17Error("this image's rescue stub is not the frozen one (sha256 "
                            f"{RESCUE_SHA256[:12]}...): the stub never changes, so its "
                            "rescue-lark.S must not either")
    s.values(setup(raw=True))
    if stub is not None and not rescue:
        _check_rescue(s, stub)
    if RESCUE < start < FW0_SYS + FW0_SYS_SIZE:
        _check_stub_behind(s)
    changed = 0
    for addr in range(start, start + len(cells), SECTOR):
        want = cells[addr - start:addr - start + SECTOR]
        if _rom_read(s, addr, SECTOR) == want:
            continue
        if s.values(Ops().call(ROM_NOR_ERASE, 0, addr, SECTOR))[0]:
            raise Ts17Error(f"ROM erase failed at {addr:06x}")
        if want != b"\xff" * SECTOR:
            s.link.write(COPY_BUF, want)
            if s.values(Ops().call(ROM_NOR_WRITE, 0, addr, COPY_BUF, SECTOR))[0]:
                raise Ts17Error(f"ROM write failed at {addr:06x}")
        if _rom_read(s, addr, SECTOR) != want:
            raise Ts17Error(f"verify failed at {addr:06x}")
        changed += 1
        if progress:
            progress(f"wrote {addr:06x}")
    return changed
