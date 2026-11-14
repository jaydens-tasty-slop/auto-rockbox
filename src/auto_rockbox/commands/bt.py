"""`arb bt`: Bluetooth range runs from this PC, and `arb yp3 bt-walk`, the
headset walk's live log over the player's dev shell (auto_rockbox.bt).

Every command writes into the run directory it is given, under fixed names
(auto_rockbox.bt.rundir), so `arb bt report` finds them.

    [bluetooth]
    player = "AA:BB:CC:DD:EE:FF"    # the player's BD_ADDR: the default ADDR
    sink = "arb_null"               # bt watch moves the player's stream here; make it
                                    # with pactl load-module module-null-sink sink_name=arb_null
"""

from __future__ import annotations

import os
import shlex
import sys
from argparse import Namespace
from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path

from ..bt import bluez, flow, gaps, report, rundir, walk
from ..errors import UsageError
from ..registry import Context, arg, command, group
from ..workspace import ConfigError


def _player(ctx: Context, given: str | None) -> str:
    addr = given or ctx.ws.section("bluetooth").get("player")
    if not isinstance(addr, str):
        raise ConfigError("no ADDR given and no [bluetooth] player address")
    return addr


def _when(text: str, day: date) -> datetime:
    try:
        return rundir.parse_when(text, day)
    except ValueError as e:
        raise UsageError(str(e)) from None


def _say(text: str) -> None:
    print(f"{rundir.clock(rundir.now())} {text}")


def gaplog(ctx: Context, args: Namespace) -> int:
    """Records the player's A2DP stream into this PC (its BlueZ input node,
    with pw-record; no audio is kept) and logs into OUT/gaps.txt each
    second's level and every run of exact-zero samples of at least --min-ms.
    When the node goes (the link or stream is lost) it is logged and waited
    for again. Ctrl-C stops it."""
    gaps.gaplog(args.out, args.addr or ctx.ws.section("bluetooth").get("player"), args.min_ms)
    return 0


def gaps_cmd(ctx: Context, args: Namespace) -> int:
    """The exact-zero runs in a 16-bit recording of the player's stream
    (e.g. `arb bt-peer pcsink`'s stream.wav). START is the time of its first sample:
    `stat -c %w FILE` gives the file's birth, which is close."""
    start = _when(args.start, date.today())
    with ExitStack() as stack:
        log = stack.enter_context(rundir.Log(args.out / rundir.GAPS)) if args.out else None

        def write(text: str, at: datetime) -> None:
            if log:
                log(text, at, quiet=text.startswith("level"))
            elif not text.startswith("level"):
                print(f"{rundir.clock(at)} {text}")

        if log:
            log.comment(gaps.header(args.min_ms))
        tracker = gaps.scan_wav(args.wav, start, write, args.min_ms)
    print(tracker.summary())
    return 0


def flow_cmd(ctx: Context, args: Namespace) -> int:
    """The A2DP media flow in this PC's HCI trace: link events, the ACL
    fragment and media packet sizes, SBC frames per packet, RTP sequence
    skips, KB/s, and the stalls between media packets. FROM TO (local
    HH:MM:SS on the trace's first day, or ISO) limit it to a window."""
    if len(args.window) not in (0, 2):
        raise UsageError("arb bt flow: FROM and TO, or neither")
    lo = hi = None
    if args.window:
        first = flow.first_time(args.trace)
        day = flow.when(first).date() if first is not None else date.today()
        lo, hi = (_when(w, day).timestamp() for w in args.window)
    print("\n".join(flow.describe(flow.read(args.trace, lo, hi), rows=args.rows)))
    return 0


def capture(ctx: Context, args: Namespace) -> int:
    """The PC's side of a player walk until Ctrl-C: btmon's HCI trace into
    OUT/pc.btsnoop and BlueZ's RSSI and TX power for the link every --period
    s into OUT/pc-rssi.txt. Needs root (btmon, btmgmt); the files are given
    back to the sudo user."""
    addr = _player(ctx, args.addr)
    if os.geteuid() != 0:
        argv = [sys.executable, "-m", "auto_rockbox.cli", "--workspace", str(ctx.ws.root),
                "bt", "capture", str(args.out.resolve()), "--addr", addr,
                "--period", f"{args.period:g}"]
        print("arb bt capture: btmon and btmgmt need root; run\n  sudo " + shlex.join(argv),
              file=sys.stderr)
        return 2
    print(f"capturing into {args.out}; Ctrl-C to stop")
    bluez.capture(args.out, addr, args.period)
    return 0


def page(ctx: Context, args: Namespace) -> int:
    """Finds, trusts and connects a player waiting discoverable (stock on
    its Bluetooth menu: it cannot connect to a PC by itself). A player that
    is not paired yet needs a pairing agent on this PC."""
    return 0 if bluez.page(_player(ctx, args.addr), args.tries, _say) else 1


def watch(ctx: Context, args: Namespace) -> int:
    """Logs the link to the player as BlueZ sees it into OUT/conn.txt (up,
    down, back) and pages the player after a drop, as stock players do not
    page again by themselves: every 20 s for --page-for s, then gives up
    (exit 1). Keeps the player's stream on --sink. Ctrl-C stops it."""
    addr = _player(ctx, args.addr)
    sink = args.sink or ctx.ws.section("bluetooth").get("sink")
    if sink and bluez.sink_index(sink) is None:
        raise UsageError(f"no sink {sink}: pactl load-module module-null-sink sink_name={sink}")
    with rundir.Log(args.out / rundir.CONN) as log:
        log.comment(f"link to {addr} every {args.period:g} s, paged for {args.page_for:g} s "
                    "after a drop")
        for ev in bluez.watch_link(addr, period=args.period, page_for=args.page_for,
                                   sink=sink):
            if ev.kind != "poll":
                log(ev.kind, ev.at)
            if ev.kind == "over":
                return 1
    return 0


def report_cmd(ctx: Context, args: Namespace) -> int:
    """One timeline and a summary from a run directory's files: seconds
    silent and the gaps by size, the first stutter, each drop with the time
    to the re-page, the link and audio, the RSSI both sides saw, the
    player's bitpool and buffer waits, the media flow. --from/--to: local
    HH:MM:SS on the run's first day, or ISO."""
    day = report.first_day(args.run)
    lo = _when(args.start, day) if args.start else None
    hi = _when(args.end, day) if args.end else None
    print("\n".join(report.report(args.run, lo, hi)))
    return 0


def bt_walk(ctx: Context, args: Namespace) -> int:
    """The player stays on the desk on USB (a dev build with usb=1),
    streaming to headphones a person carries along a route. Holds the dev
    shell for the whole walk and writes into OUT/walk.txt, stamped, every
    line the player sends, its Bluetooth and audio state as it changes, and
    the link stats. Ends once the headset is back after a drop and playback
    resumed (exit 0) or did not within 20 s (exit 1), or at the time limit."""
    end = walk.walk(ctx.t.shell(), args.out, args.minutes, args.period)
    return 1 if end.startswith("not resumed") else 0


_OUT = arg("out", type=Path, help="the run directory")
_ADDR = arg("--addr", help="the player's BD_ADDR (default [bluetooth] player)")
_ADDR_POS = arg("addr", nargs="?", help="the player's BD_ADDR (default [bluetooth] player)")
_MIN_MS = arg("--min-ms", type=int, default=gaps.MIN_MS,
              help=f"shortest zero run that counts (default {gaps.MIN_MS})")

group("bt", help="Bluetooth range runs: the stream's gaps, the link, the HCI trace, a report")

command("bt gaplog", target=False, help="log the gaps in the player's stream, live",
        args=[_OUT, _ADDR, _MIN_MS])(gaplog)

command("bt gaps", target=False, help="the gaps in a recording of the stream", args=[
    arg("wav", type=Path),
    arg("start", help="its first sample's time: HH:MM:SS[.fff] today, or ISO"),
    _MIN_MS,
    arg("--out", type=Path, metavar="RUN", help="also write RUN/gaps.txt"),
])(gaps_cmd)

command("bt flow", target=False, help="the A2DP media flow in an HCI trace (btmon -w)", args=[
    arg("trace", type=Path, help="e.g. RUN/pc.btsnoop"),
    arg("window", nargs="*", metavar="FROM TO", help="local times, HH:MM:SS or ISO"),
    arg("--rows", action="store_true", help="also one line per second"),
])(flow_cmd)

command("bt capture", target=False, help="HCI trace and RSSI of a player walk (root)", args=[
    _OUT, _ADDR,
    arg("--period", type=float, default=2.0, help="seconds between RSSI reads (default 2)"),
])(capture)

command("bt page", target=False, help="find, trust and connect a discoverable player", args=[
    _ADDR_POS,
    arg("--tries", type=int, default=60, help="scans before giving up (default 60)"),
])(page)

command("bt watch", target=False, help="log the link and page the player after a drop", args=[
    _OUT, _ADDR_POS,
    arg("--period", type=float, default=2.0, help="seconds between polls (default 2)"),
    arg("--page-for", type=float, default=bluez.PAGE_FOR_S,
        help=f"seconds to page after a drop; 0: never page, for a player that reconnects "
        f"by itself (default {bluez.PAGE_FOR_S:g})"),
    arg("--sink", help="keep the player's stream on this sink (default [bluetooth] sink)"),
])(watch)

command("bt report", target=False, help="one timeline and a summary of a run directory", args=[
    arg("run", type=Path),
    arg("--from", dest="start", metavar="TIME", help="from this local time"),
    arg("--to", dest="end", metavar="TIME", help="up to this local time"),
])(report_cmd)

command("bt-walk", needs={"shell"},
        help="the headset walk's live log over the dev shell", args=[
    _OUT,
    arg("--minutes", type=float, default=15.0, help="the walk's time limit (default 15)"),
    arg("--period", type=float, default=5.0,
        help=f"seconds between {walk.LINK!r} polls (default 5)"),
])(bt_walk)
