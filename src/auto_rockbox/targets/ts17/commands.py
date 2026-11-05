"""The TS17's own commands, all from download mode: the NOR flash, memory,
resets, and the way back from stock firmware."""

from __future__ import annotations

import sys
import time
from argparse import Namespace
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ...registry import Context, arg, command, group
from . import adfu, gadget, nor, power, rdm, stock
from .gadget import Ops, Session

WAIT_S = 30.0                   # a command may be typed while the player is coming back

SCB = {                         # Cortex-M4 system control registers
    "CPUID": 0xE000ED00, "ICSR": 0xE000ED04, "VTOR": 0xE000ED08, "AIRCR": 0xE000ED0C,
    "CCR": 0xE000ED14, "SHCSR": 0xE000ED24, "CFSR": 0xE000ED28, "HFSR": 0xE000ED2C,
    "ACTLR": 0xE000E008, "SYST_CSR": 0xE000E010, "ID_PFR0": 0xE000ED40,
    "ID_ISAR0": 0xE000ED60, "CPACR": 0xE000ED88, "MVFR0": 0xE000EF40,
    "ICTR": 0xE000E004, "ISER0": 0xE000E100, "ISER1": 0xE000E104, "ISER2": 0xE000E108,
}
REG_BLOCKS = ((nor.NOR_CTL, 16), (nor.CACHE_CTL, 4), (nor.REMAP0, 16), (power.WDT_CTL, 8))


def number(s: str) -> int:
    return int(s, 0)


@contextmanager
def _session(ctx: Context) -> Generator[Session]:
    """The device lock and a gadget session, with the charger guarded first"""
    with ctx.t.lock(), gadget.connect(ctx.ws, WAIT_S) as s:
        print(power.guard(s).describe(), file=sys.stderr)
        yield s


def _progress(msg: str) -> None:
    print(f"\r{msg}", end="", file=sys.stderr, flush=True)


def _write(ctx: Context, start: int, cells: bytes, boot: bool) -> None:
    with _session(ctx) as s:
        n = nor.write_cells(s, start, cells, boot=boot, progress=print)
    print(f"{n} of {len(cells) // nor.SECTOR} sectors rewritten; "
          f"{start:06x}+{len(cells):x} matches")


def nor_ident(ctx: Context, args: Namespace) -> None:
    """CPU (SCB), NOR controller, cache, remap and watchdog registers;
    "(fault)" marks a register whose read faulted."""
    with _session(ctx) as s:
        ops = Ops()
        for addr in SCB.values():
            ops.r32(addr)
        r = s.run(ops)
        for i, ((name, addr), v) in enumerate(zip(SCB.items(), r.values, strict=True)):
            print(f"{name:9} {addr:08x} = {v:08x}{'  (fault)' if i in r.fault_ops else ''}")
        for base, n in REG_BLOCKS:
            ops = Ops()
            for i in range(n):
                ops.r32(base + 4 * i)
            vals = s.values(ops)
            for i in range(0, n, 4):
                print(f"{base + 4 * i:08x}  {' '.join(f'{v:08x}' for v in vals[i:i + 4])}")


def nor_map(ctx: Context, args: Namespace) -> None:
    """64 bytes of NOR at OFFSET through the read window."""
    with _session(ctx) as s:
        s.values(nor.setup(args.raw))
        ops = nor.map_window(Ops(), args.offset, args.crypt)
        for i in range(16):
            ops.r32(nor.WINDOW + 4 * i)
        data = b"".join(v.to_bytes(4, "little") for v in s.values(ops)[-16:])
    for i in range(0, len(data), 16):
        print(f"{args.offset + i:06x}  {data[i:i + 16].hex(' ')}")


def nor_dump(ctx: Context, args: Namespace) -> None:
    """NOR to FILE, 4 MiB in about 2 s. With --raw it is a backup that
    `restore` can write back."""
    t0 = time.monotonic()
    with _session(ctx) as s, args.file.open("wb") as f:
        nor.read(s, args.start, args.length, f, raw=args.raw, crypt=args.crypt,
                 progress=_progress)
    print(f"\n{args.length // 1024} KiB in {time.monotonic() - t0:.1f} s -> {args.file}")


def nor_restore(ctx: Context, args: Namespace) -> None:
    """Makes flash match a cell image (a `dump --raw`) at the same offsets:
    START to the image's end, or LENGTH bytes. Without IMAGE: stock's
    fw0_sys from [targets.ts17] stock_cells. Rewrites only the 4 KiB
    sectors that differ, and verifies each."""
    image, start, length = args.image, args.start, args.length
    if image is None:
        image = ctx.t.conf.opt_path("stock_cells")
        if image is None:
            raise SystemExit("no IMAGE, and [targets.ts17] stock_cells is not set")
        start, length = nor.FW0_SYS, nor.FW0_SYS_SIZE
    data = image.read_bytes()
    end = len(data) if length is None else start + length
    if not start < end <= len(data):
        raise SystemExit(f"{image} ({len(data):#x} bytes) has no {start:#x}-{end:#x}")
    _write(ctx, start, data[start:end], args.boot)


def nor_program(ctx: Context, args: Namespace) -> None:
    """A plain file at START, encoded for XIP reads (the randomizer on); the
    rest of its last sector is left erased. Rewrites only the 4 KiB sectors
    that differ, and verifies each."""
    data = args.file.read_bytes()
    _write(ctx, args.start, nor.pad(rdm.encode(nor.pad(data, rdm.XIP_RUN)), nor.SECTOR),
           args.boot)


WINDOW_ARGS = [arg("--raw", action="store_true", help="the stored cells (randomizer off)"),
               arg("--crypt", action="store_true", help="set the remap's crypt bit (31)")]
BOOT_ARG = arg("--boot", action="store_true", help="allow the boot sectors below 0x4000")
TS17 = {"ts17"}

group("nor", help="the NOR flash: ident, map, dump, restore, program")
command("nor ident", target=TS17,
        help="CPU, NOR controller, cache, remap and watchdog registers")(nor_ident)
command("nor map", target=TS17, help="64 bytes at a NOR offset",
        args=[arg("offset", type=number), *WINDOW_ARGS])(nor_map)
command("nor dump", target=TS17, help="NOR to a file",
        args=[arg("file", type=Path), arg("start", type=number), arg("length", type=number),
              *WINDOW_ARGS])(nor_dump)
command("nor restore", target=TS17, help="write back a --raw dump (changed sectors only)",
        args=[arg("image", type=Path, nargs="?"),
              arg("start", type=number, nargs="?", default=0),
              arg("length", type=number, nargs="?"), BOOT_ARG])(nor_restore)
command("nor program", target=TS17, help="write a plain file, encoded for XIP",
        args=[arg("file", type=Path), arg("start", type=number), BOOT_ARG])(nor_program)


@command("reboot", target=TS17, help="reset into download mode, or into stock firmware",
         args=[arg("--stock", action="store_true",
                   help="clear the boot-mode magic: boots stock (or whatever is at fw0_sys)")])
def reboot(ctx: Context, args: Namespace) -> int:
    """A watchdog reset the way stock reboots."""
    with _session(ctx) as s:
        s.hand_over(power.reset_ops(download=not args.stock), adfu.present())
    print("reset sent; expect " + ("the boot chain (stock: 10d6:b00b)" if args.stock
                                   else "download mode (10d6:10d6) in about 1 s"))
    return 0


@command("mem", target=TS17, help="read the player's memory into a file", args=[
    arg("addr", type=number), arg("size", type=number), arg("file", type=Path)])
def mem(ctx: Context, args: Namespace) -> int:
    """Plain ROM reads: no gadget runs, so RAM is as the last run left it
    (the framebuffer at 0x01040000, the dev block at 0x01143000)."""
    with ctx.t.lock(), adfu.Adfu(WAIT_S) as link:
        data = link.read(args.addr, args.size)
    args.file.write_bytes(data)
    print(f"read {args.size:#x} bytes from {args.addr:#010x} into {args.file}")
    return 0


@command("stock-adfu", target=TS17, help="stock firmware's USB disk into download mode")
def stock_adfu(ctx: Context, args: Namespace) -> int:
    """Needed when a reset booted stock: it has no hook to return by itself."""
    with ctx.t.lock():
        t = stock.to_download_mode()
    print(f"download mode after {t:.1f} s")
    return 0
