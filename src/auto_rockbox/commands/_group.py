"""Host commands with actions: `arb linein serve`, `arb bench curve`, ...

The registry knows one level of commands, so a group registers one command
whose first argument names the action; the rest go to that action's own
parser, which gives each action its own arguments and --help.
"""

from __future__ import annotations

import argparse
import inspect
from argparse import Namespace
from collections.abc import Sequence
from dataclasses import dataclass

from ..registry import Arg, CommandFn, Context, arg, command


@dataclass(frozen=True)
class Action:
    fn: CommandFn
    help: str
    args: Sequence[Arg] = ()


def group(name: str, help: str, actions: dict[str, Action]) -> None:
    def run(ctx: Context, args: Namespace) -> int | None:
        action = actions[args.action]
        p = argparse.ArgumentParser(prog=f"arb {name} {args.action}",
                                    description=inspect.cleandoc(action.fn.__doc__ or action.help),
                                    formatter_class=argparse.RawDescriptionHelpFormatter)
        for a in action.args:
            p.add_argument(*a.flags, **a.kwargs)
        return action.fn(ctx, p.parse_args(args.argv))

    width = max(map(len, actions))
    run.__doc__ = help + "\n\nactions (ACTION --help for each one's arguments):\n" + "\n".join(
        f"  {k:{width}}  {a.help}" for k, a in actions.items())
    command(name, help=help, target=False, args=[
        arg("action", choices=list(actions), metavar="ACTION"),
        arg("argv", nargs=argparse.REMAINDER, help=argparse.SUPPRESS),
    ])(run)
