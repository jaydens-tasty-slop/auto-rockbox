"""The player's card over Rockbox's USB mass storage, for every player with
the `storage` capability: install a build's files, run a host command on
the mounted card, check the filesystem."""

from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from argparse import Namespace
from pathlib import Path

from .. import card
from ..errors import UsageError
from ..registry import Context, arg, command

SECONDS = arg("--seconds", type=int, default=600,
              help="the player returns after this long; must outlast the copy (default 600)")
IMAGE = arg("--image", type=Path,
            help="the build to boot as USB mass storage (default: the target's configured one)")


@command("card-push", help="unzip a Rockbox zip (rockbox.zip) onto the card", needs={"storage"},
         args=[arg("zip", type=Path), SECONDS, IMAGE,
               arg("--check", action="store_true", help="fsck the card afterwards")])
def card_push(ctx: Context, args: Namespace) -> int:
    """Files the zip does not have are left alone."""
    with zipfile.ZipFile(args.zip) as z:
        bad = [n for n in z.namelist() if n.startswith("/") or ".." in Path(n).parts]
        if bad:
            raise UsageError(f"{args.zip}: unsafe member {bad[0]!r}")
        with ctx.t.mass_storage(args.seconds, args.image) as dev:
            with card.mounted(dev) as mp:
                print(f"card at {mp}; unzipping {len(z.namelist())} files", flush=True)
                z.extractall(mp)
            if args.check:
                print("fsck:", card.check(dev))
    return 0


@command("card-run", help="run a shell command with the card mounted at $MP",
         needs={"storage"}, args=[arg("cmd", help="e.g. 'cp -r build/rocks/.rockbox \"$MP/\"'"),
                                  SECONDS, IMAGE,
                                  arg("--check", action="store_true",
                                      help="fsck the card afterwards")])
def card_run(ctx: Context, args: Namespace) -> int:
    with ctx.t.mass_storage(args.seconds, args.image) as dev:
        with card.mounted(dev) as mp:
            print(f"card at {mp}", flush=True)
            rc = subprocess.run(["bash", "-c", args.cmd], env={**os.environ, "MP": str(mp)},
                                check=False).returncode
        if args.check:
            print("fsck:", card.check(dev))
    if rc:
        print(f"command exited {rc}", file=sys.stderr)
    return rc


@command("card-check", help="fsck the card (udisks' filesystem check)", needs={"storage"},
         args=[SECONDS, IMAGE])
def card_check(ctx: Context, args: Namespace) -> int:
    with ctx.t.mass_storage(args.seconds, args.image) as dev:
        reply = card.check(dev)
    print(reply)
    return 0 if "true" in reply else 1
