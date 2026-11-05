"""`arb webcam`: publish the bench camera's frames, set its focus."""

from __future__ import annotations

import dataclasses
import sys
from argparse import Namespace
from pathlib import Path

from .. import webcam
from ..publish import PublisherBusy
from ..registry import Context, arg
from ._group import Action, group


def serve(ctx: Context, args: Namespace) -> int:
    """Publishes a frame a second to [webcam] latest (see auto_rockbox.webcam).
    Ctrl-C stops it."""
    s = webcam.Settings.of(ctx.ws)
    changes = {k: v for k, v in (("device", args.device), ("size", args.size),
                                 ("history", args.history)) if v is not None}
    if args.focus is not None:
        changes["focus"] = webcam.parse_focus(args.focus)
    try:
        webcam.serve(dataclasses.replace(s, **changes))
    except (OSError, EOFError, ValueError, TimeoutError, webcam.FocusError,
            PublisherBusy) as e:
        print(f"arb webcam serve: {e}", file=sys.stderr)
        return 1
    return 0


def focus(ctx: Context, args: Namespace) -> int:
    """Without N: the focus now and the camera's range. With N (its
    focus_absolute, or "auto"): sets it, reaching it from the minimum; a
    running publisher keeps streaming and reports the change. --sweep: frames
    at several settings from the running publisher, ending at N (default
    [webcam] focus)."""
    s = webcam.Settings.of(ctx.ws)
    try:
        f = webcam.Focus(args.device or s.device)
        if args.sweep:
            value = webcam.parse_focus(args.value) if args.value else s.focus
            if value is None:
                raise webcam.FocusError("a sweep needs a manual focus to end at")
            webcam.sweep(f, s.latest, value, args.out or s.latest.parent / "focus")
        elif args.value:
            print(f.set(webcam.parse_focus(args.value)))
        else:
            r = f.range
            print(f"{f.read()}; range {r.minimum}-{r.maximum}, step {r.step}")
    except (ValueError, webcam.FocusError) as e:
        print(f"arb webcam focus: {e}", file=sys.stderr)
        return 1
    return 0


group("webcam", help="the bench camera: publish a frame a second, set its focus", actions={
    "serve": Action(serve, "publish frames (a daemon)", [
        arg("--device", help="V4L2 device (default [webcam] device, /dev/video0)"),
        arg("--size", help="WIDTHxHEIGHT (default [webcam] size, 1280x720)"),
        arg("--focus", metavar="N|auto",
            help=f"focus_absolute (default [webcam] focus, {webcam.DEFAULT_FOCUS})"),
        arg("--history", type=int, metavar="FRAMES",
            help="frames kept in history/ (default [webcam] history, 0)"),
    ]),
    "focus": Action(focus, "read or set the focus, or sweep it", [
        arg("value", nargs="?", metavar="N|auto"),
        arg("--device", help="V4L2 device (default [webcam] device)"),
        arg("--sweep", action="store_true", help="save frames at several focus settings"),
        arg("--out", type=Path, help="where --sweep saves (default focus/ beside latest)"),
    ]),
})
