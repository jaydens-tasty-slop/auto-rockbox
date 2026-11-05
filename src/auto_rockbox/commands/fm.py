"""FM bring-up checks on recordings (skills/fm-radio.md)."""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

from .. import fmcheck
from ..registry import Context, arg, command


@command("fm-check", target=False,
         help="an FM recording: overload wraps, level, stereo correlation",
         args=[arg("wav", type=Path, nargs="+"),
               arg("--step", type=float, default=fmcheck.WRAP_STEP,
                   help="a wrap is a jump over this fraction of full scale "
                        f"between neighbouring samples (default {fmcheck.WRAP_STEP})")])
def fm_check(ctx: Context, args: Namespace) -> int:
    """Exit status 1 if any file has overload wraps."""
    bad = False
    for p in args.wav:
        r = fmcheck.check(p, args.step)
        stereo = ("mono file" if r.correlation is None else
                  f"L/R correlation {r.correlation:.3f}, side {r.side_db:+.1f} dB re mid")
        print(f"{p}: {r.seconds:.1f} s, peak {r.peak_dbfs:.1f} dBFS, rms {r.rms_dbfs:.1f} dBFS, "
              f"{stereo}, {r.wraps} overload wraps"
              + (f" at {', '.join(f'{t:.2f}' for t in r.wrap_times)} s" if r.wraps else ""))
        bad |= r.wraps > 0
    return 1 if bad else 0
