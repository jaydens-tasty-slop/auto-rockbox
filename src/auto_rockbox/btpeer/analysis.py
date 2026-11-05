"""A received stream's audio (a WAV file) second by second: level, tone and
purity per channel, the median tone, and the dropouts over the whole file,
with Line In's analysis (auto_rockbox.linein) on each piece."""

from __future__ import annotations

import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import linein

LEAD_WINDOWS = 100          # a low run this short at the very start is not a gap: the
                            # stream can start before the player's audio (the stack sends
                            # silence until its mixer has PCM: ~46 ms in the simulator)
EDGE_WINDOWS = 5            # nor one this short at the very end


@dataclass(frozen=True)
class WavAnalysis:
    rate: int
    channels: int
    seconds: float
    medians: list[float | None]             # each channel's median tone, Hz
    gaps: list[tuple[float, float]]         # (start s, length ms)
    edges: list[tuple[int, int]]            # ignored low runs at the ends, in windows


def _merged_runs(runs: list[tuple[int, int]], n: int) -> list[tuple[int, int]]:
    """The runs of windows that are low on any channel, as (start, length)"""
    low = np.zeros(n + 2, np.int8)
    for start, length in runs:
        low[start + 1:start + 1 + length] = 1
    edges = np.flatnonzero(np.diff(low))
    starts, ends = edges[0::2], edges[1::2]
    return list(zip(starts.tolist(), (ends - starts).tolist(), strict=True))


def analyse_wav(path: Path, out: Callable[[str], object] = print) -> WavAnalysis:
    """Per second and channel: RMS dBFS, dominant Hz and tone purity (to OUT);
    a channel's tone is the median over its clean, audible seconds. A
    dropout is a run of 1 ms windows below half the file's median level on
    any channel."""
    with wave.open(str(path)) as w:
        rate, nch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, nch)
    x = a.astype(np.float64) / 32768
    peaks: list[list[float]] = [[] for _ in range(nch)]
    out(f"per second ({rate} Hz, {nch} ch): RMS dBFS, dominant Hz, tone purity")
    for s in range(len(x) // rate):
        chans = [linein.analyse(x[s * rate:(s + 1) * rate, ch], rate) for ch in range(nch)]
        out(f"  {s:4d} s  " + " | ".join(map(str, chans)))
        for ch, c in enumerate(chans):
            if c.db > linein.SILENT_DB and c.purity > linein.CLEAN:
                peaks[ch].append(c.hz)
    medians = [float(np.median(p)) if p else None for p in peaks]
    for ch, m in enumerate(medians):
        out(f"  ch{ch}: median tone "
            + (f"{m:.1f} Hz over {len(peaks[ch])} s" if m else "none"))
    window = rate // 1000
    n = len(x) // window
    runs = _merged_runs([r for ch in range(nch) for r in linein.low_runs(x[:, ch], rate)], n)
    edges = [r for r in runs if (r[0] == 0 and r[1] <= LEAD_WINDOWS)
             or (r[0] + r[1] == n and r[1] <= EDGE_WINDOWS)]
    ms = 1000 * window / rate
    gaps = [(start * ms / 1000, length * ms) for start, length in runs
            if (start, length) not in edges]
    return WavAnalysis(rate, nch, len(x) / rate, medians, gaps, edges)
