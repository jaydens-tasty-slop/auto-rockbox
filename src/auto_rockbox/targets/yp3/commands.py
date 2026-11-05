"""The YP3's own commands: flashing and inspection in ROM USB, the decoders of
what a run leaves in retained SRAM, the dev shell's screenshot, the stock
firmware, the charge log and the profiler. `run` replaces the generic one to
add files for the card and extra flash content.
"""

from __future__ import annotations

import sys
import time
from argparse import Namespace
from pathlib import Path

from ... import fb
from ...elf import Elf
from ...registry import Context, arg, command
from ...run import parse_settings, report, run
from . import Yp3, Yp3Run, prof
from .image import ImageError, parse_blob, parse_push
from .power import ChargeLog
from .retained import decode_fault, decode_threads, trace_names
from .rom import IMAGE_SIZE, PMU_REASON, PMU_RESET, PMU_WAKE, Op, wait_rom
from .shot import screenshot

YP3 = {"yp3"}


def _yp3(ctx: Context) -> Yp3:
    t = ctx.t
    if not isinstance(t, Yp3):
        raise TypeError(f"{t.name}: not a YP3")
    return t


def num(s: str) -> int:
    return int(s, 0)


def _pushes(t: Yp3, args: Namespace, elf: Path) -> list[tuple[Path, str]]:
    pushes = [parse_push(p) for p in args.push]
    return pushes + (t.modules(elf) if args.modules else [])


IMAGE_ARGS = [
    arg("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE",
        help="a dev config line (apps/dev_shell.c: ls= play= volume= monitor= crash= usb= "
             "speaker= poweroff= prof= ...)"),
    arg("--push", action="append", default=[], metavar="LOCAL=/CARD/PATH",
        help="copy LOCAL to the card at boot when it differs (652 KiB in all)"),
    arg("--blob", action="append", default=[], metavar="FILE@OFFSET",
        help="also write FILE into the flash at OFFSET, in [0xe000, 0x150000)"),
    arg("--modules", action="store_true",
        help="also push the modules of [targets.yp3] modules from the ELF's build dir"),
    arg("--release", action="store_true",
        help="allow an ELF that is not a YP3_DEV build (it never returns by itself)"),
]
ELF_ARG = arg("elf", type=Path, nargs="?",
              help="names the addresses (default: [targets.yp3] elf)")


@command("run", help="flash an image, boot it, wait for its return, print its log",
         target=YP3, args=[
             arg("image", type=Path, help="usually the build's rockbox.elf (packaged against "
                 "the base image); a 2 MiB image file is flashed as it is"),
             arg("seconds", type=int, nargs="?", default=60,
                 help="the image stops feeding the watchdog after this long and is back in "
                      "ROM USB about 17 s later; 0: it stays up (default 60)"),
             arg("--keys", default="", help='a key script, e.g. "S 1500 D S" (arb yp3 keys)'),
             *IMAGE_ARGS,
             arg("--usb", action="store_true", help="start USB with the dev shell (usb=1)"),
             arg("--shots", type=Path, metavar="DIR",
                 help="webcam frames during the run and a contact sheet there"),
         ])
def run_cmd(ctx: Context, args: Namespace) -> int:
    """Charger guard, package and flash the changed sectors, boot, then wait
    for the watchdog return and print the dev log. Exit status 3 if the
    player did not come back in time. --set lines go to the dev config as
    they are; the return time and key script come from SECONDS and --keys."""
    t = _yp3(ctx)
    req = Yp3Run(args.image, args.seconds, args.keys, parse_settings(args.settings), usb=args.usb,
                 pushes=_pushes(t, args, args.image),
                 blobs=[parse_blob(b) for b in args.blob], release=args.release)
    r = run(t, req, args.shots)
    if req.seconds == 0:
        print("it stays up (return=0)")
        return 0
    return report(t, req, r)


@command("flash", help="write an image's changed sectors (an ELF is packaged first)",
         target=YP3, args=[arg("image", type=Path), *IMAGE_ARGS])
def flash(ctx: Context, args: Namespace) -> int:
    """Only [0xe000, 0x1fc000) is written: the bootloader, the partition table
    and this player's settings stay. The dev config is the --set lines alone."""
    t = _yp3(ctx)
    pushes = _pushes(t, args, args.image)
    blobs = [parse_blob(b) for b in args.blob]
    with t.session() as rom:
        rom.flash(t.image(args.image, parse_settings(args.settings), pushes=pushes, blobs=blobs,
                          release=args.release, rom=rom))
    return 0


@command("package", help="make the flash image for an ELF, without the player",
         target=YP3, args=[arg("elf", type=Path), arg("out", type=Path), *IMAGE_ARGS])
def package(ctx: Context, args: Namespace) -> int:
    """Needs the base image ([targets.yp3] base_image); the dev config is the
    --set lines alone"""
    t = _yp3(ctx)
    img = t.image(args.elf, parse_settings(args.settings), pushes=_pushes(t, args, args.elf),
                  blobs=[parse_blob(b) for b in args.blob], release=args.release)
    args.out.write_bytes(img)
    print(args.out)
    return 0


@command("boot", help="reset into the flashed image (clears the recovery reason)",
         target=YP3)
def boot(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        rom.reset(Op.BOOT)
    print("reset sent")
    return 0


@command("rom", help="reset into a fresh ROM USB (arms the recovery reason)", target=YP3)
def rom_cmd(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        old = rom.reset(Op.RESET_TO_ROM)
        print("reset sent")
        ident = wait_rom(time.monotonic() + 30, old)
    print(f"rom {ident.name}" if ident else "did not return")
    return 0 if ident else 1


@command("wait", help="wait for the player in ROM USB", target=YP3,
         args=[arg("seconds", type=float, nargs="?", default=60.0)])
def wait(ctx: Context, args: Namespace) -> int:
    ident = wait_rom(time.monotonic() + args.seconds)
    if ident is None:
        print("timeout: no ROM USB", file=sys.stderr)
        return 1
    print(f"rom {ident.name} (bus {ident.bus} device {ident.dev})")
    return 0


@command("state", help="the payload's view (MSP, IPSR, SPI, ...) and the PMU reset flags",
         target=YP3)
def state(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        info = rom.info()
        flags = [rom.pmu_read(i) for i in (PMU_REASON, PMU_WAKE, PMU_RESET)]
    print(" ".join(f"{n}={v:#x}" for n, v in info.items()))
    print("pmu23={:#x} pmu00={:#x} pmu20={:#x}".format(*flags))
    return 0


@command("read-flash", help="read flash through the payload into a file", target=YP3,
         args=[arg("addr", type=num), arg("length", type=num, help="a multiple of 64"),
               arg("out", type=Path)])
def read_flash(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        args.out.write_bytes(rom.read_flash(args.addr, args.length))
    return 0


@command("read-ram", help="read SRAM through the ROM's own reader into a file", target=YP3,
         args=[arg("addr", type=num), arg("length", type=num), arg("out", type=Path)])
def read_ram(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        args.out.write_bytes(rom.read_ram(args.addr, args.length))
    return 0


@command("r32", help="read a word", target=YP3, args=[arg("addr", type=num)])
def r32(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        print(hex(rom.read32(args.addr)))
    return 0


@command("w32", help="write a word (prints it read back)", target=YP3,
         args=[arg("addr", type=num), arg("value", type=num)])
def w32(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        print(hex(rom.write32(args.addr, args.value)))
    return 0


@command("pmu", help="read (or write, then read) a PMU register", target=YP3,
         args=[arg("index", type=num), arg("value", type=num, nargs="?")])
def pmu(ctx: Context, args: Namespace) -> int:
    with _yp3(ctx).session() as rom:
        if args.value is not None:
            rom.pmu_write(args.index, args.value)
        print(hex(rom.pmu_read(args.index)))
    return 0


@command("fault", help="decode the fault record the last run left", target=YP3,
         args=[ELF_ARG])
def fault(ctx: Context, args: Namespace) -> int:
    t = _yp3(ctx)
    with t.lock():
        raw = t.read_retained()
    elf = args.elf or t.default_elf
    print(decode_fault(raw, t.symbolizer(elf).names if elf.exists() else None))
    return 0


@command("threads", help="the scheduler snapshot the last run left (after a hang)",
         target=YP3, args=[ELF_ARG])
def threads(ctx: Context, args: Namespace) -> int:
    """Threads (state, priority, wait object, return addresses), the running
    one, the tick tasks, and the first failed slot check if any. Taken every
    0.1 s and frozen at the first bad slot id."""
    t = _yp3(ctx)
    elf = Elf(args.elf or t.default_elf)
    with t.lock():
        raw = t.read_retained()
    print(decode_threads(raw, elf, t.function_names(elf.path)))
    return 0


@command("sym", help="name tick-trace words (slot + 1 in the top byte, PC below)",
         target=YP3, args=[arg("words", nargs="+", metavar="HEX"),
                           arg("--elf", type=Path, help="default: [targets.yp3] elf")])
def sym(ctx: Context, args: Namespace) -> int:
    t = _yp3(ctx)
    print(trace_names([int(w, 16) for w in args.words], t.function_names(args.elf)))
    return 0


@command("shot", help="screenshot of the running player through the dev shell",
         target=YP3, needs={"shell"},
         args=[arg("out", type=Path, nargs="?", help="PNG, 3x (default: <build>/shot.png)")])
def shot(ctx: Context, args: Namespace) -> int:
    """Needs a run with --usb. A sleeping LCD shows its last frame: send a key
    first. Exit status 1 if rows are still missing after refetching them."""
    t = _yp3(ctx)
    out = args.out or t.ws.build_dir / "shot.png"
    sh = t.shell()
    im, missing = screenshot(lambda line: sh(line, timeout=60), t.lcd)
    out.parent.mkdir(parents=True, exist_ok=True)
    fb.scaled(im).save(out)
    print(f"{out}: {im.width}x{im.height}" + (f", missing rows {missing}" if missing else ""))
    return 1 if missing else 0


@command("stock", help="flash this player's stock firmware back and boot it", target=YP3)
def stock(ctx: Context, args: Namespace) -> int:
    """From [targets.yp3] stock_image (its first 2 MiB: a dump may repeat
    with that period). Only [0xe000, 0x1fc000) is written, so the bootloader
    and this player's settings stay. Back to Rockbox: Down+RESET into ROM
    USB, then run."""
    t = _yp3(ctx)
    if "stock_image" not in t.conf.data:
        raise ImageError("[targets.yp3] stock_image: a dump of this player's stock firmware "
                         "is needed")
    data = t.conf.path("stock_image").read_bytes()[:IMAGE_SIZE]
    if len(data) < IMAGE_SIZE:
        raise ImageError(f"stock_image: {len(data)} bytes, a flash image is {IMAGE_SIZE}")
    with t.session() as rom:
        rom.flash(data)
        rom.reset(Op.BOOT)
    print("stock firmware booting")
    return 0


@command("charge-log", help="charge from ROM USB as stock does; a CSV line a minute",
         target=YP3, args=[
             arg("out", type=Path, help="CSV, appended: utc, elapsed_s, kind, mv, pmu17, "
                 "pmu2a, pmu30"),
             arg("--wait-hours", type=float, default=12.0,
                 help="how long to wait for ROM USB first (default 12)"),
             arg("--target-mv", type=int,
                 help="charge on past full until a rest reading reaches this, then stop "
                      "with the charger off"),
             arg("--max-hours", type=float,
                 help="stop charging after this long (default 8, or 6 with --target-mv)"),
         ])
def charge_log(ctx: Context, args: Namespace) -> int:
    """Reads the cell as found, then charges with the charger switched on
    until the PMU's charge-full event, pausing it a minute every 10 minutes
    for a near-rest reading ("rest" rows), then logs 30 minutes of rest.
    The device lock is held only while sampling."""
    ChargeLog(_yp3(ctx).session, args.out, args.target_mv, args.max_hours).run(args.wait_hours)
    return 0


@command("profile", help="map `sh prof dump` PC samples to functions", target=YP3,
         args=[arg("prof", type=Path, help="the saved output of `arb yp3 sh prof dump`"),
               ELF_ARG])
def profile(ctx: Context, args: Namespace) -> int:
    """`sh prof start`, use the player, then `sh prof dump > FILE`. Tick
    samples (100 Hz) miss work threads do right after the tick; `prof start
    irq 62` samples at the audio DMA interrupt instead (wired output playing)."""
    pcs = prof.samples(args.prof.read_text())
    if not pcs:
        raise ImageError(f"{args.prof}: no samples")
    print(prof.report(pcs, Elf(args.elf or _yp3(ctx).default_elf).functions))
    return 0
