"""The dev loop's commands, for every target."""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

from .. import fb, registry
from ..errors import UsageError
from ..registry import Context, arg, command
from ..run import parse_settings, report, run
from ..target import RunRequest


@command("targets", help="list the players this install knows", target=False)
def targets(ctx: Context, args: Namespace) -> int:
    for name, cls in registry.targets().items():
        caps = ", ".join(sorted(cls.capabilities)) or "-"
        print(f"{name:10} {cls.summary}\n{'':10} capabilities: {caps}")
    return 0


@command("run", help="flash or load an image, run it, print its log", args=[
    arg("image", type=Path, help="usually the build's rockbox.elf"),
    arg("seconds", type=int, nargs="?", default=60,
        help="the image returns to the host after this long (default 60)"),
    arg("--keys", default="", help='a key script, e.g. "9000 D D S" (arb TARGET keys)'),
    arg("--set", dest="settings", action="append", default=[], metavar="KEY=VALUE",
        help="a run setting the target's dev build reads (target-defined)"),
    arg("--cold", action="store_true", help="boot through the normal boot chain"),
    arg("--usb", action="store_true", help="let Rockbox start its USB stack"),
    arg("--shots", type=Path, metavar="DIR",
        help="webcam frames during the run, then fb.png and sheet.png there"),
])
def run_cmd(ctx: Context, args: Namespace) -> int:
    """Puts IMAGE on the player with a run config (return time, key script,
    settings), starts it, waits for it to come back to the host and prints
    the log it left. Exit status 3 if it did not come back in time."""
    req = RunRequest(args.image, args.seconds, args.keys, parse_settings(args.settings),
                     args.cold, args.usb)
    t = ctx.t
    if req.cold and not t.has("cold"):
        raise UsageError(f"{t.name}: no --cold boot")
    if req.usb and not t.has("usb"):
        raise UsageError(f"{t.name}: no --usb")
    return report(t, req, run(t, req, args.shots))


@command("log", help="print the dev log the last run left",
         args=[arg("elf", type=Path, nargs="?", help="the ELF that ran (names addresses)")])
def log(ctx: Context, args: Namespace) -> int:
    with ctx.t.lock():
        text = ctx.t.read_log(args.elf)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


@command("fb", help="save the framebuffer the last run left as a PNG", needs={"framebuffer"},
         args=[arg("image", type=Path, nargs="?", help="the ELF that ran (finds the buffer)"),
               arg("--out", type=Path, default=Path("fb.png")),
               arg("--scale", type=int, default=3)])
def fb_cmd(ctx: Context, args: Namespace) -> int:
    with ctx.t.lock():
        im = ctx.t.read_framebuffer(args.image)
    fb.scaled(im, args.scale).save(args.out)
    print(args.out)
    return 0


@command("keys", help="check a key script and say how long it takes",
         args=[arg("script")])
def keys_cmd(ctx: Context, args: Namespace) -> int:
    ctx.t.check_keys(args.script)
    print(f"ok, about {ctx.t.key_duration_ms(args.script) / 1000:.1f} s")
    return 0


@command("sh", help="one dev shell command on the running player", needs={"shell"},
         args=[arg("line", nargs="+"), arg("--timeout", type=float, default=120.0)])
def sh(ctx: Context, args: Namespace) -> int:
    reply = ctx.t.shell()(" ".join(args.line), args.timeout, echo=print)
    if reply.timed_out:
        print("timeout", file=sys.stderr)
    return 0 if reply.ok else 1


@command("battery", help="the cell's voltage and the charger", needs={"battery"})
def battery(ctx: Context, args: Namespace) -> int:
    with ctx.t.lock():
        b = ctx.t.battery()
    print(f"{b.millivolts} mV, {'charging' if b.charging else 'not charging'}"
          + (f"; {b.note}" if b.note else ""))
    return 0
