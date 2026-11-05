"""Spectra and tone measurements, numpy only (no scipy)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import numpy as np


def db(x: np.ndarray | float, floor: float = 1e-12) -> np.ndarray:
    return 20 * np.log10(np.maximum(np.abs(x), floor))


def bh4(n: int) -> np.ndarray:
    """4-term Blackman-Harris window: sidelobes under -92 dB, so a tone's
    leakage does not read as distortion or noise"""
    a = (0.35875, 0.48829, 0.14128, 0.01168)
    k = 2 * np.pi * np.arange(n) / (n - 1)
    return a[0] - a[1] * np.cos(k) + a[2] * np.cos(2 * k) - a[3] * np.cos(3 * k)


def a_weight(f: np.ndarray) -> np.ndarray:
    """IEC 61672 A-weighting as an amplitude factor, 1 at 1 kHz"""
    f2 = np.asarray(f, float) ** 2
    ra = (12194 ** 2 * f2 ** 2) / ((f2 + 20.6 ** 2) * np.sqrt(
        (f2 + 107.7 ** 2) * (f2 + 737.9 ** 2)) * (f2 + 12194 ** 2))
    return ra / 0.7943282347242815


def dbfs(power: float) -> float:
    """Mean-square power in dBFS, a full-scale sine being 0"""
    return float(10 * np.log10(max(power, 1e-30) * 2))


class Spectrum:
    """Windowed power spectrum, per bin; power() sums a tone's main lobe"""

    def __init__(self, x: np.ndarray, fs: float) -> None:
        n = len(x)
        w = bh4(n)
        spec = np.fft.rfft((x - x.mean()) * w)
        self.p = 2 * np.abs(spec) ** 2 / (n * (w ** 2).sum())
        self.f = np.fft.rfftfreq(n, 1 / fs)
        self.df = fs / n
        self.fs = fs

    def idx(self, f: float, lobe: int = 6) -> slice:
        k = round(f / self.df)
        return slice(max(k - lobe, 0), k + lobe + 1)

    def peak(self, f: float, search: float = 0.03) -> float:
        """The strongest bin within SEARCH (a fraction) of F: the player's
        clock moves its tones"""
        lo, hi = int(f * (1 - search) / self.df), int(f * (1 + search) / self.df) + 1
        return float((lo + np.argmax(self.p[lo:hi])) * self.df)

    def power(self, f: float) -> float:
        return float(self.p[self.idx(f)].sum())

    def band(self, lo: float = 20, hi: float = 20000,
             weight: Callable[[np.ndarray], np.ndarray] | None = None) -> float:
        m = (self.f >= lo) & (self.f <= min(hi, self.fs / 2 * 0.98))
        return float((self.p[m] * (weight(self.f[m]) ** 2 if weight else 1)).sum())


def tone_metrics(x: np.ndarray, fs: float, freq: float) -> dict[str, Any]:
    """Level (dBFS), THD (H2..H10), THD+N (20 Hz-20 kHz) and H2..H5 (dB re
    the fundamental) of one tone"""
    sp = Spectrum(x, fs)
    top = min(20000, fs / 2 * 0.98)
    f0 = sp.peak(freq)
    p1 = sp.power(f0)
    harm = [sp.power(k * f0) for k in range(2, 11) if k * f0 < top]
    mask = np.ones_like(sp.p, bool)
    mask[sp.idx(f0)] = False
    resid = sp.p[mask & (sp.f >= 20) & (sp.f <= top)].sum()
    return {"freq": f0, "level": dbfs(p1),
            "thd": 10 * np.log10(sum(harm) / p1 + 1e-30) if harm else None,
            "thdn": 10 * np.log10(resid / p1 + 1e-30),
            "h": [10 * np.log10(h / p1 + 1e-30) for h in harm[:4]],
            "total": dbfs(sp.band())}


def log_grid(f1: float = 20.0, f2: float = 20000.0, per_oct: int = 48) -> np.ndarray:
    n = int(np.ceil(np.log2(f2 / f1) * per_oct)) + 1
    return f1 * 2 ** (np.arange(n) / per_oct)


def smooth_mag(f: np.ndarray, mag2: np.ndarray, grid: np.ndarray, frac: int = 6) -> np.ndarray:
    """1/FRAC-octave power average of |H|^2 (sampled at linear F) at GRID"""
    c = np.concatenate([[0], np.cumsum(mag2)])
    h = 2 ** (1 / (2 * frac))
    lo = np.minimum(np.searchsorted(f, grid / h), len(f) - 1)
    hi = np.minimum(np.maximum(np.searchsorted(f, grid * h), lo + 1), len(f))
    return (c[hi] - c[lo]) / (hi - lo)


def arr(v: Sequence[float | None]) -> np.ndarray:
    """A JSON list (None for missing) as floats with NaN"""
    return np.array([np.nan if e is None else e for e in v], float)


def norm_at(grid: np.ndarray, y: np.ndarray, f0: float = 1000.0) -> np.ndarray:
    """Y relative to its value at F0 (log-interpolated)"""
    return y - np.interp(np.log(f0), np.log(grid), y)
