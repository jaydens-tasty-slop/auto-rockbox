"""An analysis written out: result JSON, report and graphs in one directory.
Graphs are skipped, with a note, when matplotlib is missing."""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path

import numpy as np

from . import analyze, plots
from .signal import Timeline

Echo = Callable[[str], object]


def analyse_to(x: np.ndarray, fs: int, tl: Timeline, label: str, out: Path, *,
               source: str, echo: Echo = print) -> analyze.Result:
    """OUT/result.json, report.txt, fr.png, thdn_level.png, hd_freq.png"""
    out.mkdir(parents=True, exist_ok=True)
    r = analyze.analyse(x, fs, tl, label)
    r["source"] = source
    (out / "result.json").write_text(json.dumps(r))
    text = analyze.report(r)
    (out / "report.txt").write_text(text + "\n")
    echo(text)
    try:
        plots.all_of([r], out)
    except plots.PlotsUnavailable as e:
        echo(f"(no graphs: {e})")
    return r


def volsteps_to(x: np.ndarray, fs: int, label: str, out: Path, *,
                volumes: Sequence[float] | None, source: str,
                echo: Echo = print) -> analyze.Result:
    """OUT/volsteps.json and volsteps.png"""
    out.mkdir(parents=True, exist_ok=True)
    r = analyze.volsteps(x, fs, label, volumes=volumes)
    r["source"] = source
    if volumes and len(volumes) != len(r["steps"]):
        echo(f"warning: {len(r['steps'])} plateaus, {len(volumes)} volumes given")
    (out / "volsteps.json").write_text(json.dumps(r, indent=1))
    for s in r["steps"]:
        echo(f"{s.get('volume', '-')!s:>6} t={s['start']:6.1f}s " + " | ".join(
            f"{c['level']:7.2f} dBFS THD+N {c['thdn']:6.1f} dB" for c in s["ch"]))
    try:
        plots.volsteps([r], out / "volsteps.png")
    except plots.PlotsUnavailable as e:
        echo(f"(no graph: {e})")
    return r
