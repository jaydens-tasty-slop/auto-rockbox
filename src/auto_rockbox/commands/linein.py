"""`arb linein`: publish this PC's Line In recording, and read it back."""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import numpy as np

from .. import linein
from ..publish import PublisherBusy
from ..registry import Context, arg, command, group

# How a command names Line In seconds; `arb audio` takes the same
WINDOW_ARGS = [
    arg("window", nargs="*", metavar="SECONDS | FROM TO",
        help="the last SECONDS (default 60), or a UTC window: HH:MM:SS[.fff] or ISO"),
    arg("--seq", nargs=2, type=int, metavar=("FIRST", "LAST"),
        help="seconds by sequence number instead"),
]


def selected(ctx: Context, args: Namespace) -> list[linein.Second]:
    """The seconds WINDOW_ARGS name, oldest first; a skip in their sequence
    numbers is reported, as the ring may have overwritten some"""
    try:
        seconds = linein.select(linein.history(linein.directory(ctx.ws)), args.window, args.seq)
    except ValueError as e:
        raise SystemExit(f"arb linein: {e} (SECONDS, or FROM TO)") from None
    for a, b in linein.gaps_in(seconds):
        print(f"(seconds missing between #{a} and #{b})", file=sys.stderr)
    return seconds


def serve(ctx: Context, args: Namespace) -> int:
    """Records a PulseAudio/PipeWire source and publishes each second (see
    auto_rockbox.linein). Sets the source to its base volume (unity gain).
    Ctrl-C stops it."""
    if args.list:
        print(linein.describe(linein.sources()))
        return 0
    conf = ctx.ws.section("linein")
    try:
        linein.serve(linein.directory(ctx.ws), args.history or conf.integer("history", 300),
                     source=args.source or conf.get("source"),
                     port=args.port or conf.get("port"), level_log=args.level_log)
    except (linein.CaptureError, PublisherBusy) as e:
        print(f"arb linein serve: {e}", file=sys.stderr)
        return 1
    return 0


def analyse(ctx: Context, args: Namespace) -> int:
    """Each loud second's level, dominant frequency and purity per channel
    (L | R), then the median tone frequency per channel. Seconds are named
    by sequence number and a stamp to the ms."""
    peaks: list[list[float]] = [[], []]
    for s in selected(ctx, args):
        h = linein.hear(s)
        if not h.loud:
            continue
        print(h.levels_line())
        for c, ch in enumerate(h.channels[:2]):
            if ch.purity > linein.CLEAN:
                peaks[c].append(ch.hz)
    for c, p in enumerate(peaks):
        if p:
            print(f"ch{c}: median tone {np.median(p):.1f} Hz over {len(p)} s")
    return 0


def gaps(ctx: Context, args: Namespace) -> int:
    """Dropouts in a steady signal, per second: 1 ms windows of the left
    channel below half the second's median RMS, as (start ms, ms) runs"""
    for s in selected(ctx, args):
        print(linein.hear(s).low_line())
    return 0


group("linein", help="Line In: publish this PC's recording of a player, analyse it")

command("linein serve", target=False, help="record and publish each second (a daemon)", args=[
    arg("--list", action="store_true", help="list the recording sources and ports"),
    arg("--source", help="pactl source name (default [linein] source, or the one "
        "with a line port)"),
    arg("--port", help="input port (default [linein] port, or its only line port)"),
    arg("--history", type=int, help="seconds the ring keeps (default [linein] history, 300)"),
    arg("--level-log", type=Path, metavar="CSV",
        help="also append a level line per second (battery runs: arb bench end)"),
])(serve)

command("linein analyse", target=False, help="level and tone per second", args=WINDOW_ARGS)(analyse)

command("linein gaps", target=False, help="dropouts per second", args=WINDOW_ARGS)(gaps)

