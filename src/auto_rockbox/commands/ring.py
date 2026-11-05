"""`arb ring`: call the person at the bench."""

from __future__ import annotations

import sys
from argparse import Namespace

from .. import attention
from ..registry import Context, arg, command


@command("ring", help="call the person at the bench (or, with --listen, wait for calls)",
         target=False, args=[
             arg("message", nargs="?", help="what they should come and do"),
             arg("--listen", action="store_true",
                 help="in the desktop session: play a sound and print each call"),
         ])
def ring(ctx: Context, args: Namespace) -> int:
    """Leaves a request that `arb ring --listen`, running in the person's
    desktop session, announces with a sound (see auto_rockbox.attention)"""
    directory, sound = attention.settings(ctx.ws)
    if args.listen == bool(args.message):
        print("arb ring: a MESSAGE or --listen", file=sys.stderr)
        return 2
    if args.message:
        attention.ring(directory, args.message)
        return 0
    try:
        attention.listen(directory, sound)
    except RuntimeError as e:
        print(f"arb ring: {e}", file=sys.stderr)
        return 1
    return 0
