"""Audio quality: a player's analog output against a known-good reference.

Both devices play the same synthesised test signal (signal.py) into the same
Line In, so the capture chain's own response cancels in the difference. The
analysis (analyze.py) finds the sync chirps, corrects the clock offset
between player and capture, and measures frequency response, harmonic
distortion, THD+N per level, noise, IMD and crosstalk; eq.py fits a Rockbox
EQ preset that makes the player match the reference; slips.py finds
whole-sample slips (clicks) in a steady tone. Analysis is numpy only; graphs
need the `plots` extra (matplotlib).

    [audio]
    out = "build/audio"     # outputs (default: the build directory's audio/)
"""

from __future__ import annotations

from pathlib import Path

from ..workspace import Workspace


def out_dir(ws: Workspace) -> Path:
    return ws.section("audio").path("out", str(ws.build_dir / "audio"))
