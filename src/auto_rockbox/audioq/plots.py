"""Graphs of audio-quality results. matplotlib is optional (the `plots`
extra): it is imported only here, only when a graph is drawn."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

from .analyze import Result, fr_curve
from .dsp import arr
from .eq import Fit, predicted

if TYPE_CHECKING:
    from matplotlib.figure import Figure

INSTALL = ("graphs need matplotlib: `uv sync --extra plots` in auto-rockbox, "
           "or pip install 'auto-rockbox[plots]'")
COLORS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
GREY, LIGHT = "#888884", "#ececea"


class PlotsUnavailable(Exception):
    pass


def require() -> None:
    """Fails early, before a long analysis, if graphs cannot be drawn"""
    try:
        import matplotlib  # noqa: F401, PLC0415
    except ImportError:
        raise PlotsUnavailable(INSTALL) from None


def _figure(figsize: tuple[float, float] = (10, 5.6)) -> Figure:
    """A figure without pyplot's global state (no display, no close needed)"""
    require()
    import matplotlib  # noqa: PLC0415
    from matplotlib.figure import Figure  # noqa: PLC0415
    matplotlib.rcParams.update({
        "font.size": 10, "axes.grid": True, "grid.color": "#d8d8d4", "grid.linewidth": 0.6,
        "axes.edgecolor": GREY, "axes.spines.top": False, "axes.spines.right": False,
        "lines.linewidth": 2, "legend.frameon": False})
    return Figure(figsize=figsize, dpi=110)


def _freq_axis(ax: Any, lo: float = 20, hi: float = 20000) -> None:
    ax.set_xscale("log")
    ax.set_xlim(lo, hi)
    ticks = [t for t in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000)
             if lo <= t <= hi]
    ax.set_xticks(ticks)
    ax.set_xticklabels([f"{t // 1000}k" if t >= 1000 else str(t) for t in ticks])
    ax.grid(True, which="minor", color=LIGHT, linewidth=0.4)
    ax.set_xlabel("Frequency (Hz)")


def _save(fig: Figure, path: Path) -> None:
    fig.tight_layout()
    fig.savefig(path)


def fr(results: Sequence[Result], path: Path, ch: str = "avg", sweep: str = "12",
       predict: tuple[str, Path] | None = None) -> None:
    """Responses (1/6 octave, normalised at 1 kHz) and each one's difference
    to the first, the reference. PREDICT (label, preset .cfg) adds that
    result with the preset applied, to check against an "after" run."""
    fig = _figure((10, 7.5))
    ax, bx = fig.subplots(2, 1, sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    curves = [(r["label"], *fr_curve(r, ch, sweep), "-") for r in results]
    if predict:
        r = next(r for r in results if r["label"] == predict[0])
        curves.append((f"{predict[0]} + EQ (predicted)", *predicted(r, predict[1], ch, sweep),
                       "--"))
    ref = curves[0]
    for i, (lab, g, y, ls) in enumerate(curves):
        c = COLORS[i % len(COLORS)]
        ax.plot(g, y, ls, color=c, label=lab)
        if i:
            bx.plot(g, y - ref[2], ls, color=c, label=f"{lab} - {ref[0]}")
    ax.set_ylabel("Level re 1 kHz (dB)")
    ax.set_title(f"Frequency response, {'L/R mean' if ch == 'avg' else ch}, "
                 f"-{sweep} dBFS sweep, 1/6-octave smoothed")
    lo = min(-6, np.floor(min(np.nanmin(c[2]) for c in curves)))
    hi = max(3, np.ceil(max(np.nanmax(c[2]) for c in curves)))
    ax.set_ylim(max(lo, -30), min(hi + 1, 15))
    ax.legend(loc="lower center", ncol=2)
    bx.axhline(0, color=GREY, linewidth=1)
    bx.set_ylabel(f"Difference re {ref[0]} (dB)")
    if len(curves) > 1:
        bx.legend(loc="lower center", ncol=2)
        d = np.concatenate([c[2] - ref[2] for c in curves[1:]])
        m = max(3, np.ceil(np.nanmax(np.abs(d[np.isfinite(d)]))))
        bx.set_ylim(-min(m, 20), min(m, 20))
    _freq_axis(bx)
    _save(fig, path)


def thdn(results: Sequence[Result], path: Path) -> None:
    fig = _figure()
    ax = fig.subplots()
    for i, r in enumerate(results):
        x = [s["dbfs_in"] for s in r["steps"]]
        for c, ls in ((0, "-"), (1, ":")):
            ax.plot(x, [s["ch"][c]["thdn"] for s in r["steps"]], ls, marker="o", ms=5,
                    color=COLORS[i % len(COLORS)], label=f"{r['label']} {'LR'[c]}")
    ax.axhline(-40, color=GREY, linewidth=1)
    ax.text(-60, -39, "1%", color="#555552", fontsize=9)
    ax.set_xlabel("Input level (dBFS, 1 kHz)")
    ax.set_ylabel("THD+N re fundamental (dB)")
    ax.set_title("THD+N vs level at 1 kHz (20 Hz-20 kHz)")
    ax.legend(ncol=2)
    _save(fig, path)


def hd(results: Sequence[Result], path: Path, ch: str = "L", sweep: str = "3") -> None:
    fig = _figure()
    ax = fig.subplots()
    for i, r in enumerate(results):
        d = r["sweeps"][sweep][ch][ch]
        for n, ls in ((2, "-"), (3, "--")):
            ax.plot(np.array(r["grid"]), arr(d[f"hd{n}"]), ls, color=COLORS[i % len(COLORS)],
                    label=f"{r['label']} H{n}")
    ax.set_ylabel("Harmonic re fundamental (dB)")
    ax.set_title(f"Harmonic distortion vs fundamental frequency, {ch}, -{sweep} dBFS sweep")
    ax.set_ylim(-110, -10)
    ax.legend(ncol=2)
    _freq_axis(ax, 20, 10000)
    _save(fig, path)


def volsteps(results: Sequence[Result], path: Path) -> None:
    """THD+N and output level per volume step, and THD+N against output
    level: the gain-staging view (at the same loudness, which split is cleaner)"""
    fig = _figure((10, 10))
    ax, bx, cx = fig.subplots(3, 1)
    bx.sharex(ax)
    for i, r in enumerate(results):
        st = r["steps"]
        x = [s.get("volume", k) for k, s in enumerate(st)]
        level = [s["ch"][0]["level"] for s in st]
        noise = [s["ch"][0]["thdn"] for s in st]
        c = COLORS[i % len(COLORS)]
        ax.plot(x, noise, "-o", ms=5, color=c, label=r["label"])
        bx.plot(x, level, "-o", ms=5, color=c, label=r["label"])
        cx.plot(level, noise, "-o", ms=5, color=c, label=r["label"])
    for a in (ax, cx):
        a.axhline(-40, color=GREY, linewidth=1)
        a.set_ylabel("THD+N, L (dB)")
    ax.set_title("1 kHz -3 dBFS tone per volume step")
    ax.legend()
    bx.set_ylabel("Output level, L (dBFS)")
    labelled = bool(results[0]["steps"]) and "volume" in results[0]["steps"][0]
    bx.set_xlabel("Volume setting (dB)" if labelled else "Step")
    cx.set_xlabel("Output level, L (dBFS capture)")
    _save(fig, path)


def all_of(results: Sequence[Result], out: Path, ch: str = "avg", sweep: str = "12",
           predict: tuple[str, Path] | None = None) -> list[Path]:
    """fr.png, thdn_level.png and hd_freq.png in OUT"""
    paths = [out / "fr.png", out / "thdn_level.png", out / "hd_freq.png"]
    fr(results, paths[0], ch, sweep, predict)
    thdn(results, paths[1])
    hd(results, paths[2], "L" if ch == "avg" else ch)
    return paths


def fit(f: Fit, path: Path) -> None:
    fig = _figure()
    ax = fig.subplots()
    ax.plot(f.grid, f.target, color=COLORS[0], label="target (reference - device)")
    ax.plot(f.grid, f.response + f.offset, color=COLORS[1], label="EQ response (+ level offset)")
    ax.plot(f.grid, f.target - f.response - f.offset, color=COLORS[2], label="residual")
    ax.axvspan(20, f.fmin, color=LIGHT)
    ax.axvspan(f.fmax, 20000, color=LIGHT)
    ax.set_ylabel("dB")
    ax.set_title(f"EQ fit: {f.dev} → {f.ref}")
    ax.legend()
    _freq_axis(ax)
    _save(fig, path)
