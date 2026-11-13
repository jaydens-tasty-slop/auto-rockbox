"""`arb bench`: battery runs (level log, run end, Bluetooth watch, discharge table)."""

from __future__ import annotations

import sys
from argparse import Namespace
from datetime import UTC, datetime
from pathlib import Path

from .. import attention, bench, linein
from ..registry import Context, arg, command, group


def _utc(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def level_log(ctx: Context, args: Namespace) -> int:
    """Appends "utc,second,L rms,L peak,R rms,R peak" (dBFS; empty fields:
    digital silence) a second to LOG, from a PulseAudio/PipeWire source: a
    Bluetooth run's null sink monitor (SINK.monitor), say. The Line In
    publisher writes the same with `arb linein serve --level-log`."""
    try:
        bench.level_log(args.source, args.log, args.rate)
    except linein.CaptureError as e:
        print(f"arb bench level-log: {e}", file=sys.stderr)
        return 1
    return 0


def end(ctx: Context, args: Namespace) -> int:
    """Exit 0 and print the run's hours once LOG shows QUIET silent seconds
    after T0 (UTC ISO, the run's start); exit 1 while it still plays"""
    r = bench.run_end(args.log, _utc(args.t0), args.quiet)
    if r is None:
        return 1
    print(r)
    return 0


def match(ctx: Context, args: Namespace) -> int:
    """Compares the run's first seconds of music from T0 with the reference
    run's (REF from REF_T0) over the same stretch of the playlist. Exit 0:
    same music within the tolerance; 3: not; 1: too few seconds logged yet"""
    try:
        m = bench.level_match(args.log, _utc(args.t0), args.ref, _utc(args.ref_t0),
                              seconds=args.seconds)
    except (OSError, ValueError, bench.BenchError) as e:
        print(f"arb bench match: {e}", file=sys.stderr)
        return 1
    if m is None:
        print(f"fewer than {args.seconds} s logged from {args.t0}", file=sys.stderr)
        return 1
    print(m)
    return 0 if m.ok(args.tolerance) else 3


def watch(ctx: Context, args: Namespace) -> int:
    """Follows a run from Play to its end, the way to watch every run. It
    prints t0 (--t0, or the first loud second after --after, default now),
    then the run's first 10 min against --ref (the reference run's level log
    and t0, e.g. stock's), then the hours once QUIET s are silent. Exit 0:
    the run is over; 3: the level or the music is not the reference's (stop
    the run; the attention cue rings)."""
    ref = (Path(args.ref[0]), _utc(args.ref[1])) if args.ref else None
    after = _utc(args.after) if args.after else datetime.now(UTC)
    try:
        rc = bench.watch(args.log, after, ref=ref, t0=_utc(args.t0) if args.t0 else None,
                         tolerance=args.tolerance, quiet=args.quiet)
    except (OSError, ValueError, bench.BenchError) as e:
        print(f"arb bench watch: {e}", file=sys.stderr)
        return 1
    if rc == 3:
        attention.ring(attention.settings(ctx.ws)[0],
                       f"battery run {args.log.name}: level is not the reference's, stop it")
    return rc


def bt_watch(ctx: Context, args: Namespace) -> int:
    """Watches a Bluetooth run: logs "utc,connected" to LOG every 10 s, keeps
    the player's stream on the null sink, and after a drop pages the player
    for 5 min. Exit 0: the run is over; 2: a range drop (check the run).
    Hold off sleep meanwhile (systemd-inhibit)."""
    sink = args.sink or ctx.ws.section("bench").string("sink", "arb_null")
    try:
        return bench.bt_watch(args.addr, args.log, sink)
    except bench.BenchError as e:
        print(f"arb bench bt-watch: {e}", file=sys.stderr)
        return 1


def curve(ctx: Context, args: Namespace) -> int:
    """The percent_to_volt_discharge table (0..100 %) from a Rockbox battery
    log. The run is its longest boot and ends at its power-off. t0: where
    the reading first falls to MV (a run started above full is cut to a
    normal full charge), or --start/--end, host times of the run's start
    and end, which place t0 that long before the power-off."""
    try:
        boot = bench.run_boot(args.battlog)
        if args.from_mv is not None:
            if args.start or args.end:
                raise bench.BenchError("--from-mv or --start/--end, not both")
            t0 = bench.crossing(boot, args.from_mv)
        elif args.start and args.end:
            t0 = boot.end - (_utc(args.end) - _utc(args.start)).total_seconds()
        else:
            raise bench.BenchError("t0 needs --from-mv, or --start and --end")
        table = bench.discharge_table(boot, t0, args.window)
    except (OSError, ValueError, bench.BenchError) as e:
        print(f"arb bench curve: {e}", file=sys.stderr)
        return 1
    off = "power-off" if boot.poweroff is not None else "last sample"
    print(f"{len(boot.uptime)} samples, t0 at {t0:.0f} s, end ({off}) at {boot.end:.0f} s: "
          f"{(boot.end - t0) / 3600:.2f} h")
    print("percent_to_volt_discharge (0..100 %):")
    print("    " + ", ".join(map(str, table)))
    return 0


group("bench", help="battery runs: level log, run end, Bluetooth watch, discharge table")

command("bench level-log", target=False, help="log a source's level each second", args=[
    arg("source", help="pactl source, e.g. arb_null.monitor"),
    arg("log", type=Path),
    arg("--rate", type=int, default=bench.A2DP_RATE, help="sample rate (default 44100)"),
])(level_log)

command("bench end", target=False, help="has the run ended? (from a level log)", args=[
    arg("log", type=Path),
    arg("t0", help="the run's start, UTC ISO"),
    arg("--quiet", type=int, default=300, metavar="S",
        help="silent seconds that end it (default 300)"),
])(end)

_TOLERANCE = arg("--tolerance", type=float, default=bench.MATCH_DB, metavar="DB",
                  help=f"largest level difference allowed (default {bench.MATCH_DB})")

command("bench match", target=False, help="a run's first minutes against the reference run's",
        args=[
    arg("log", type=Path),
    arg("t0", help="the run's start, UTC ISO"),
    arg("ref", type=Path, help="the reference run's level log"),
    arg("ref_t0", help="the reference run's start, UTC ISO"),
    arg("--seconds", type=int, default=bench.MATCH_S, metavar="S",
        help=f"seconds compared (default {bench.MATCH_S})"),
    _TOLERANCE,
])(match)

command("bench watch", target=False,
        help="follow a run: t0, its level against the reference, its end", args=[
    arg("log", type=Path),
    arg("--ref", nargs=2, metavar=("LOG", "T0"), type=str,
        help="the reference run's level log and start (UTC ISO)"),
    arg("--t0", metavar="ISO", help="the run's start, if already known"),
    arg("--after", metavar="ISO", help="t0 is the first loud second after this (default now)"),
    _TOLERANCE,
    arg("--quiet", type=int, default=300, metavar="S",
        help="silent seconds that end it (default 300)"),
])(watch)

command("bench bt-watch", target=False, help="watch a Bluetooth run's link", args=[
    arg("addr", help="the player's Bluetooth address"),
    arg("log", type=Path),
    arg("--sink", help="null sink for its stream (default [bench] sink, arb_null)"),
])(bt_watch)

command("bench curve", target=False, help="discharge table from a battery log", args=[
    arg("battlog", type=Path),
    arg("--from-mv", type=float, metavar="MV", help="t0 where the reading falls to MV"),
    arg("--start", metavar="ISO", help="the run's start (host clock)"),
    arg("--end", metavar="ISO", help="the run's end (host clock): the power-off"),
    arg("--window", type=float, default=bench.CURVE_WINDOW_S, metavar="S",
        help="median window, +-S s (default 120)"),
])(curve)

