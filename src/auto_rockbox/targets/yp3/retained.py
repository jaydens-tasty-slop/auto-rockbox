"""What a YP3_DEV build leaves in the SRAM that a watchdog reset keeps (the
top of SRAM, app.lds .yp3_retained), read from ROM USB after the return: the
fault record (fault-yp3.c) and the dev log (dev-yp3.c). The dev log is a
header, then blocks named by their first word (the tick PC trace, the second
core's trace in dual-core builds, the scheduler snapshot of kernel-sl6801.c),
then the log text ring up to the top of SRAM. Walking the blocks by magic
keeps older layouts decodable. A PMU reset (boot, rom) loses all of it.

Code addresses are named by a NAMES function (address list in, one function
name each out, "??" or "?" where unknown), usually addr2line on the ELF.
"""

from __future__ import annotations

import binascii
import re
import struct
from collections.abc import Callable

from ...elf import Elf

Names = Callable[[list[int]], list[str]]

RETAINED = (0x87E000, 0x2000)   # read from here to the top of SRAM

FAULT_MAGIC = 0x46594A59    # "YJYF": a fault record
FAULT_COMMIT = 0x434F4D54   # written last, after the CRC: the record is whole
FAULT_BYTES = 128
FAULT_CRC_BYTES = 120       # the CRC-32 covers the words before it
FAULT_FIELDS = ("magic", "version_size", "flags", "exc_return", "msp", "psp", "control", "ipsr",
                "cfsr", "hfsr", "mmfar", "bfar", "r4", "r5", "r6", "r7", "r8", "r9", "r10",
                "r11", "frame_sp", "r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr", "masks",
                "crc32", "commit")
FRAME_FIELDS = ("r0", "r1", "r2", "r3", "r12", "lr", "pc", "xpsr")

LOG_MAGIC = 0x474F4C59      # "YLOG": header {magic, head (bytes ever written), boots, reserved}
LOG_HEADER = 16
TRACE_MAGIC = 0x43525459    # "YTRC": {magic, pos, TRACE_PCS words}: PCs the tick interrupted
TRACE_PCS = 30
COP_MAGIC = 0x504F4359      # "YCOP": {magic, pos, COP_PCS words, fault {exc, pc, lr, sp}}
COP_PCS = 16
SNAP_MAGIC = 0x52485459     # "YTHR": {magic, size, ...}: the scheduler snapshot
BLOCK_BYTES = {TRACE_MAGIC: 8 + 4 * TRACE_PCS, COP_MAGIC: 8 + 4 * COP_PCS + 16}
TRACE_PC_MASK = 0xFFFFFF    # a trace word: PC below, the thread's slot + 1 in the top byte
THREAD_STATES = ("killed", "running", "blocked", "sleeping", "blocked_tmo", "frozen")
HZ = 100

NO_LOG = "no dev log (not a YP3_DEV build, or SRAM lost by a PMU reset)"


def find_magic(raw: bytes, magic: int, align: int = 8) -> int | None:
    m = struct.pack("<I", magic)
    for off in range(0, len(raw) - 4, align):
        if raw[off:off + 4] == m:
            return off
    return None


def log_blocks(raw: bytes, off: int) -> tuple[dict[int, int], int]:
    """{magic: offset} of the blocks after the dev log header at OFF, and the
    offset of the log text after them"""
    blocks: dict[int, int] = {}
    pos = off + LOG_HEADER
    while pos + 8 <= len(raw):
        magic, size = struct.unpack_from("<2I", raw, pos)
        size = size if magic == SNAP_MAGIC else BLOCK_BYTES.get(magic, 0)
        if not size or magic in blocks:
            break
        blocks[magic] = pos
        pos += size
    return blocks, pos


def _named(addrs: list[int], names: Names) -> list[str]:
    return [n if n not in ("?", "??") else f"{a:x}"
            for a, n in zip(addrs, names(addrs), strict=True)]


def trace_names(words: list[int], names: Names) -> str:
    """Tick trace words as "function@slot", consecutive repeats folded ("xN")"""
    if not words:
        return ""
    seen: list[list] = []
    for w, fn in zip(words, _named([w & TRACE_PC_MASK for w in words], names), strict=True):
        slot = w >> 24
        n = fn + (f"@{slot - 1}" if slot else "")
        if seen and seen[-1][0] == n:
            seen[-1][1] += 1
        else:
            seen.append([n, 1])
    return ", ".join(f"{n} x{c}" if c > 1 else n for n, c in seen)


def annotate_ticks(line: str, names: Names) -> str:
    """A shell `ticks:` line (an underrun's trace) with its functions named"""
    if not line.startswith("  ticks:"):
        return line
    words = [int(w, 16) for w in line.split()[1:] if re.fullmatch(r"[0-9a-f]{6,8}", w)]
    return line + "\n    = " + trace_names(words, names)


def _ring(raw: bytes, pos: int, n: int) -> list[int]:
    """The last N words of a ring at RAW whose next slot is POS, newest first"""
    pcs = struct.unpack_from(f"<{n}I", raw)
    return [pcs[(pos - 1 - i) % n] for i in range(min(pos, n))]


def decode_log(raw: bytes, names: Names) -> str:
    off = find_magic(raw, LOG_MAGIC)
    if off is None:
        return NO_LOG
    head = struct.unpack_from("<I", raw, off + 4)[0]
    blocks, text_at = log_blocks(raw, off)
    trace = ""
    if TRACE_MAGIC in blocks:
        t = blocks[TRACE_MAGIC]
        last = _ring(raw[t + 8:], struct.unpack_from("<I", raw, t + 4)[0], TRACE_PCS)
        trace = ("last tick PCs (newest first): " + " ".join(f"{p:x}" for p in last)
                 + "\n  = " + trace_names(last, names))
    if COP_MAGIC in blocks:
        cop = blocks[COP_MAGIC]
        last = _ring(raw[cop + 8:], struct.unpack_from("<I", raw, cop + 4)[0], COP_PCS)
        exc, pc, lr, sp = struct.unpack_from("<4I", raw, cop + 8 + 4 * COP_PCS)
        if last:
            trace += ("\nCOP tick PCs (newest first): " + " ".join(f"{p:x}" for p in last)
                      + "\n  = " + trace_names(last, names))
        if exc:
            trace += (f"\nCOP unexpected exception {exc}: pc {pc:x} lr {lr:x} sp {sp:x}\n  = "
                      + trace_names([pc & TRACE_PC_MASK, lr & TRACE_PC_MASK], names))
    data = raw[text_at:]
    n = len(data)                   # the ring runs to the top of SRAM
    text = data[:head] if head <= n else data[head % n:] + data[:head % n]
    return text.decode("latin-1") + ("\n" + trace if trace else "")


def decode_fault(raw: bytes, names: Names | None = None) -> str:
    off = find_magic(raw, FAULT_MAGIC)
    if off is None:
        return "no fault record"
    rec = raw[off:off + FAULT_BYTES]
    if len(rec) < FAULT_BYTES:
        return "no committed fault record"
    w = dict(zip(FAULT_FIELDS, struct.unpack("<32I", rec), strict=True))
    if w["commit"] != FAULT_COMMIT:
        return "no committed fault record"
    crc = "" if binascii.crc32(rec[:FAULT_CRC_BYTES]) & 0xFFFFFFFF == w["crc32"] \
        else " (CRC mismatch)"
    lines = [f"fault record{crc}: flags={w['flags']:#x} ipsr={w['ipsr']} "
             f"cfsr={w['cfsr']:#010x} hfsr={w['hfsr']:#010x} "
             f"mmfar={w['mmfar']:#x} bfar={w['bfar']:#x}",
             " ".join(f"{k}={w[k]:#x}" for k in FRAME_FIELDS)
             + f" frame_sp={w['frame_sp']:#x} msp={w['msp']:#x} psp={w['psp']:#x}"]
    if names:
        pc, lr = names([w["pc"], w["lr"] & ~1])
        lines += [f"  pc: {pc}", f"  lr: {lr}"]
    return "\n".join(lines)


def decode_threads(raw: bytes, elf: Elf, names: Names) -> str:
    """The scheduler snapshot: each thread's id, state, wait object and return
    addresses, the running thread, the tick tasks and the first failed slot
    check. The live tables are no use: after a watchdog return the ROM has
    written over them."""
    off = find_magic(raw, LOG_MAGIC)
    blocks = log_blocks(raw, off)[0] if off is not None else {}
    if SNAP_MAGIC not in blocks:
        return "no thread snapshot (not a YP3_DEV build with one, or SRAM lost by a PMU reset)"
    s = blocks[SNAP_MAGIC]
    nslots, ntasks, nret, bad = raw[s + 8:s + 12]
    tick, bad_tick, running = struct.unpack_from("<3I", raw, s + 12)
    tasks = struct.unpack_from(f"<{ntasks}I", raw, s + 24)
    base, size = elf.symbols.get("__thread_entries", (0, 0))
    entry = size // nslots if nslots else 0

    def slot(ptr: int) -> str:
        o = ptr - base
        if base and entry and 0 <= o < size and o % entry == 0:
            return f"slot {o // entry}"
        return f"{ptr:#x}" if ptr else "none"

    def text(addr: int) -> str:
        for seg in elf.segments:
            if seg.vaddr <= addr < seg.vaddr + len(seg.data):
                at = addr - seg.vaddr
                return seg.data[at:at + 32].split(b"\0")[0].decode("latin-1")
        return f"{addr:#x}"

    lines = [f"snapshot at tick {tick} ({tick / HZ:.1f} s after boot): running {slot(running)}"]
    if bad:
        what = ("the running pointer is no slot" if bad == 0xFF else
                f"slot {bad - 1}'s id names another slot")
        lines.append(f"FAILED CHECK at tick {bad_tick}: {what}; the copy stopped there "
                     "(the dev log has the words around it)")
    for n in range(nslots):
        o = s + 24 + 4 * ntasks + n * (20 + 4 * nret)
        tid, name, sp, wqp = struct.unpack_from("<4I", raw, o)
        rets = [a for a in struct.unpack_from(f"<{nret}I", raw, o + 16) if a]
        state, prio, base_prio = raw[o + 16 + 4 * nret:o + 19 + 4 * nret]
        if not (tid or name or state):
            continue
        st = THREAD_STATES[state] if state < len(THREAD_STATES) else f"state {state}"
        line = (f"slot {n:2} id {tid:#06x}{'' if tid & 0xFF == n else ' (BAD SLOT)'} "
                f"{st} prio {prio}/{base_prio} {text(name) if name else '-'!r} sp {sp:#x}")
        if wqp:
            line += f" waits on {wqp:#x}"
        lines.append(line)
        if rets and state:
            lines.append("    " + " < ".join(_named([a - 2 for a in rets], names)))
    live = [t & ~1 for t in tasks if t]
    lines.append("tick tasks: " + (", ".join(_named(live, names)) if live else "none"))
    return "\n".join(lines)
