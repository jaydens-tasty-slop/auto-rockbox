"""`arb`: auto-rockbox's command line.

    arb targets                         the players this install knows
    arb <target> <command> [args]       e.g. arb ts17 run build/ts17/rockbox.elf 60
    arb <target> --help                 that player's commands
    arb <host-command> [args]           commands without a player (media, linein, ...)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import registry
from .errors import ArbError
from .registry import Command, Context
from .workspace import ConfigError, Workspace

type Subparsers = argparse._SubParsersAction[argparse.ArgumentParser]


def _add(sub: Subparsers, groups: dict[str, Subparsers], c: Command) -> None:
    """C's parser under SUB, or under its group's (made on first use)"""
    name = c.name
    if " " in name:
        g, name = name.split(" ", 1)
        if g not in groups:
            h = registry.group_help(g)
            gp = sub.add_parser(g, help=h, description=h)
            groups[g] = gp.add_subparsers(dest="_action", metavar="ACTION", required=True)
        sub = groups[g]
    p = sub.add_parser(name, help=c.help, description=c.description or c.help,
                       formatter_class=argparse.RawDescriptionHelpFormatter)
    for a in c.args:
        p.add_argument(*a.flags, **a.kwargs)
    p.set_defaults(_command=c)


def parser(ws: Workspace) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="arb", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--workspace", type=Path, help="the port's checkout (default: found "
                    "from the current directory by its auto-rockbox.toml)")
    top = ap.add_subparsers(dest="_top", metavar="TARGET|COMMAND", required=True)
    groups: dict[str, Subparsers] = {}
    for c in registry.host_commands().values():
        _add(top, groups, c)
    for name, cls in registry.targets().items():
        tp = top.add_parser(name, help=cls.summary, description=cls.summary)
        tp.set_defaults(_target=cls)
        sub = tp.add_subparsers(dest="_cmd", metavar="COMMAND", required=True)
        groups = {}
        for c in registry.commands_for(cls).values():
            _add(sub, groups, c)
    return ap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # --workspace must be known before plugins load: they come from it
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--workspace", type=Path)
    known, _ = pre.parse_known_args(argv)
    try:
        ws = Workspace.find(known.workspace)
        registry.load_plugins(ws)
        args = parser(ws).parse_args(argv)
        cls = getattr(args, "_target", None)
        ctx = Context(ws, cls(ws) if cls else None)
        rc = args._command.fn(ctx, args)
    except ConfigError as e:
        print(f"arb: config: {e}", file=sys.stderr)
        return 2
    except (ArbError, OSError) as e:
        if os.environ.get("ARB_TRACEBACK"):
            raise
        print(f"arb: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return rc or 0


if __name__ == "__main__":
    sys.exit(main())
