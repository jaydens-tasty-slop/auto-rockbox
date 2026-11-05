"""`arb plugins`: checks of a Rockbox tree's plugins and their builds, no
player needed: game-controller coverage (auto_rockbox.padcheck: the tree is
[rockbox] src, the manifest [rockbox] pad_manifest) and FPU use
(auto_rockbox.fpucheck)."""

from __future__ import annotations

import json
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path

from .. import fpucheck, padcheck
from ..errors import UsageError
from ..registry import Context, arg, command, group

SRC = arg("--src", type=Path, help="the Rockbox tree (default [rockbox] src, rockbox)")


def _src(ctx: Context, args: Namespace) -> Path:
    return args.src or ctx.ws.section("rockbox").path("src", "rockbox")


def _print(lines: list[str]) -> None:
    for line in lines:
        print(line)


def pad_inventory(ctx: Context, args: Namespace) -> int:
    """Each rock's sources from its linker map, with the local headers they
    include: the ones in it alone and the shared ones, which read input and
    which have pad code. Prints who reads input; --json writes it all (what
    pad-check --inventory takes)."""
    src = _src(ctx, args)
    rocks = padcheck.facts(padcheck.membership(args.build, src), src)
    if args.json:
        args.json.write_text(json.dumps({n: asdict(r) for n, r in rocks.items()}, indent=1)
                             + "\n")
    _print(padcheck.inventory(rocks))
    return 0


def pad_check(ctx: Context, args: Namespace) -> int:
    """Every rock has a manifest row whose claim its sources bear out (see
    auto_rockbox.padcheck for the manifest). Exit status 1 on any gap."""
    if (args.build is None) == (args.inventory is None):
        raise UsageError("pad-check takes a BUILD or --inventory JSON")
    src = _src(ctx, args)
    if args.build:
        rocks = padcheck.membership(args.build, src)
    else:
        rocks = {n: padcheck.Rock(r["map"], r["sources"])
                 for n, r in json.loads(args.inventory.read_text()).items()}
    manifest = args.manifest or ctx.ws.section("rockbox").path(
        "pad_manifest", "docs/plugin-pad-buttons.csv")
    report = padcheck.check(rocks, src, padcheck.load_manifest(manifest), args.only)
    _print(report.lines())
    return report.status


def pad_gate(ctx: Context, args: Namespace) -> int:
    """Every change to apps/plugins since BASE (committed or not) sits under
    HAVE_BLUETOOTH_HID: without it each file reads as BASE's; new pad code
    names no target buttons and tests no CONFIG_KEYPAD; new files fail.
    Exit status 1 on any problem."""
    report = padcheck.gate(_src(ctx, args), args.base, args.paths)
    _print(report.lines())
    return report.status


def pad_objcmp(ctx: Context, args: Namespace) -> int:
    """The plugin objects of two builds hold the same code and data (debug
    info aside): a build without HAVE_BLUETOOTH_HID against its base. Exit
    status 1 on any difference."""
    report = padcheck.objcmp(args.a, args.b, ctx.ws.tool)
    _print(report.lines())
    return report.status


def fpu(ctx: Context, args: Namespace) -> int:
    """Fails on FPU code reachable from ROOT (the plugin's interrupt-context
    callbacks and other threads) and on FP access switched in a function with
    FPU code; lists the indirect calls it cannot follow. --list: the
    functions that use the FPU."""
    funcs = fpucheck.parse(fpucheck.disassemble(args.elf, ctx.ws.tool("objdump")))
    if args.list:
        if args.roots:
            raise UsageError("--list takes no ROOT")
        _print(fpucheck.vfp_users(funcs))
        return 0
    result = fpucheck.check(funcs, args.roots)
    _print(result.lines)
    return result.status


group("plugins", help="check a Rockbox tree's plugins: game-controller coverage, FPU use")

command("plugins pad-inventory", target=False, help="the rocks' sources and who reads input",
        args=[arg("build", type=Path, help="an all-plugin build directory"), SRC,
              arg("--json", type=Path, metavar="OUT", help="write the whole inventory there")],
        )(pad_inventory)

command("plugins pad-check", target=False, help="check the rocks against the pad manifest",
        args=[arg("build", type=Path, nargs="?", help="an all-plugin build directory"),
              arg("--inventory", type=Path, metavar="JSON",
                  help="a pad-inventory --json instead of a build"),
              SRC,
              arg("--manifest", type=Path, metavar="CSV",
                  help="default [rockbox] pad_manifest, docs/plugin-pad-buttons.csv"),
              arg("--only", action="store_true", help="just the rocks the manifest names")],
        )(pad_check)

command("plugins pad-gate", target=False, help="are the plugin changes since BASE pad-only?",
        args=[arg("base", help="the commit the pad changes start from"),
              arg("paths", nargs="*", metavar="PATH",
                  help="only these (relative to apps/plugins unless they start with apps/)"),
              SRC],
        )(pad_gate)

command("plugins pad-objcmp", target=False, help="same plugin code in two builds?",
        args=[arg("a", type=Path, metavar="BUILD_A"), arg("b", type=Path, metavar="BUILD_B")],
        )(pad_objcmp)

command("plugins fpu", target=False, help="FPU code reachable from a plugin's callbacks?",
        args=[arg("elf", type=Path, help="the plugin's linked ELF (its .elf beside the .rock)"),
              arg("roots", nargs="*", metavar="ROOT",
                  help="functions that must stay soft-float, with all they call"),
              arg("--list", action="store_true", help="list the functions that use the FPU")],
        )(fpu)
