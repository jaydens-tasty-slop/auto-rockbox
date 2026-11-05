"""The TS17's 4 MiB SPI NOR from download mode, through the ops gadget.

Download mode leaves the NOR/cache block unclocked and in reset; `setup`
brings it up as the boot ROM's first stage (mbrec) does. Reads go through the
XIP cache's remap entry 2 (window 0x12000000) in 32 KiB steps, descrambled
unless `raw`. Writes call the boot ROM's own SPI-NOR routines (CALL ops),
which store bytes as given: an image for XIP is encoded first (rdm.py).
Calling ROM code is fine for tooling; Rockbox's flash driver is its own.

Flash map: boot sectors (mbrec A/B, partition tables) below 0x4000, which
writes refuse unless asked; stock's application fw0_sys at 0x14000, which
the dev loop replaces (stock comes back by restoring it from a dump of the
stored cells, `nor dump --raw`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import BinaryIO

from .adfu import Ts17Error
from .gadget import Ops, Session

FLASH_SIZE = 0x400000
SECTOR = 0x1000                 # erase unit
BOOT_END = 0x4000               # mbrec A/B and the partition tables end here
FW0_SYS = 0x14000               # stock's application: the dev image runs from here
FW0_SYS_SIZE = 0x1A7000
XIP_BASE = 0x10000000           # remap entry 0's virtual base, where fw0_sys runs

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


def _rom_read(s: Session, addr: int, n: int) -> bytes:
    s.values(Ops().call(ROM_NOR_READ, 0, addr, COPY_BUF, n))
    return s.link.read(COPY_BUF, n)


def write_cells(s: Session, start: int, cells: bytes, *, boot: bool = False,
                progress: Progress | None = None) -> int:
    """Makes flash at START match CELLS: per sector, read what is stored and
    if it differs erase, program and verify. Identical sectors are left
    alone to spare wear. Returns how many were rewritten."""
    if start % SECTOR or len(cells) % SECTOR or start + len(cells) > FLASH_SIZE:
        raise Ts17Error("a flash write must be whole 4 KiB sectors inside the flash")
    if start < BOOT_END and not boot:
        raise Ts17Error(f"the range touches the boot sectors (below {BOOT_END:#x})")
    s.values(setup(raw=True))
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
