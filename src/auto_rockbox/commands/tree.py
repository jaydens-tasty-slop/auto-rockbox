"""`arb tree`: does a port's tree change other targets' builds? (setup.tree in
the port checklist; auto_rockbox.treecmp)"""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from .. import treecmp
from ..registry import Context, arg, command, group

JOBS = 8                        # the host's CPU budget


def tree_build(ctx: Context, args: Namespace) -> int:
    """Builds TARGET from the Rockbox tree SRC (make -k, the same source path
    and version as every tree build) and copies its objects, modules, core,
    failures and module build ID to OUT. Build the port's base and its branch
    this way, then `tree compare` them."""
    failed = treecmp.build(
        args.src, args.out, ctx.ws.build_dir / "arb" / "tree", target=args.target,
        configure=args.configure, prefix=ctx.ws.tool("gcc").removesuffix("gcc"),
        jobs=args.jobs, note=lambda m: print(m, file=sys.stderr, flush=True))
    print(f"{args.out}: {len(failed)} failed" + "".join(f"\n  {f}" for f in failed))
    return 0


def tree_compare(ctx: Context, args: Namespace) -> int:
    """Every output of two tree builds, each difference classed (debug info,
    module build ID, timestamp) or DIFFERS. Exit status 1 on a difference,
    an output in one build only, or a failure in one only."""
    c = treecmp.compare(args.a, args.b, treecmp.elf_dumper(ctx.ws.tool("objcopy"),
                                                            ctx.ws.tool("objdump")))
    for line in c.lines():
        print(line)
    return c.status


group("tree", help="does a port's tree change other targets' builds?")

command("tree build", target=False, help="build a target from a Rockbox tree for comparing",
        args=[arg("src", type=Path, help="the Rockbox tree"),
              arg("out", type=Path, help="a new directory for the outputs"),
              arg("--target", required=True, help="configure's target name (sansafuzeplus)"),
              arg("--configure", default="", metavar="OPTIONS",
                  help="more configure options (--ram=1 --rbdir=/.rockbox)"),
              arg("--jobs", type=int, default=JOBS)],
        )(tree_build)

command("tree compare", target=False, help="compare two tree builds output by output",
        args=[arg("a", type=Path, metavar="BUILD_A"), arg("b", type=Path, metavar="BUILD_B")],
        )(tree_compare)
