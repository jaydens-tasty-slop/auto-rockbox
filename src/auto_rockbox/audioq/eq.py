"""Rockbox's 10-band EQ: its biquads, its .cfg presets, and a fit that makes
a player match a reference.

The biquads use lib/rbcodec/dsp/dsp_filter.c's formulas (RBJ cookbook) at
44.1 kHz, so a fitted preset predicts what the player's DSP will do. Band 0
is a low shelf, 1..8 peaks, 9 a high shelf, as Rockbox's defaults; a band at
gain 0 is off (no DSP cost).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .analyze import Result, fr_curve
from .dsp import log_grid, norm_at

EQ_FS = 44100
LS, PK, HS = "LOW_SHELF", "PEAK", "HIGH_SHELF"
KINDS = [LS] + [PK] * 8 + [HS]
NOMINAL = [32, 64, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
WEAK_DB = 0.3           # bands smaller than this are switched off: no DSP time

# (kind, centre Hz, Q*10, gain*10 dB): the .cfg's units
Band = tuple[str, float, float, float]


def biquad(kind: str, fc: float, q10: float, g10: float,
           fs: float = EQ_FS) -> tuple[np.ndarray, np.ndarray]:
    """Coefficients (b, a), normalised to a0 = 1. Q10 0 means Q 1"""
    w0 = 2 * np.pi * fc / fs
    cs, sn = np.cos(w0), np.sin(w0)
    alpha = sn / (2 * (q10 or 1) / 10)
    if kind == PK:
        a_ = 10 ** (g10 / 400)
        b = [1 + alpha * a_, -2 * cs, 1 - alpha * a_]
        a = [1 + alpha / a_, -2 * cs, 1 - alpha / a_]
    else:
        # get_replaygain_int(db*5/2): C truncation (0.01 dB units) for the
        # integer settings the player uses; continuous while fitting
        h = int(g10 * 5 / 2) if float(g10).is_integer() else g10 * 5 / 2
        a_ = (10 ** (h / 2000)) ** 2
        sa = 2 * np.sqrt(a_) * alpha
        if kind == LS:
            b = [a_ * ((a_ + 1) - (a_ - 1) * cs + sa), 2 * a_ * ((a_ - 1) - (a_ + 1) * cs),
                 a_ * ((a_ + 1) - (a_ - 1) * cs - sa)]
            a = [(a_ + 1) + (a_ - 1) * cs + sa, -2 * ((a_ - 1) + (a_ + 1) * cs),
                 (a_ + 1) + (a_ - 1) * cs - sa]
        else:
            b = [a_ * ((a_ + 1) + (a_ - 1) * cs + sa), -2 * a_ * ((a_ - 1) + (a_ + 1) * cs),
                 a_ * ((a_ + 1) + (a_ - 1) * cs - sa)]
            a = [(a_ + 1) - (a_ - 1) * cs + sa, 2 * ((a_ - 1) - (a_ + 1) * cs),
                 (a_ + 1) - (a_ - 1) * cs - sa]
    return np.array(b) / a[0], np.array(a) / a[0]


def biquad_resp(b: np.ndarray, a: np.ndarray, f: np.ndarray, fs: float = EQ_FS) -> np.ndarray:
    z = np.exp(-1j * 2 * np.pi * np.asarray(f) / fs)
    return (b[0] + b[1] * z + b[2] * z * z) / (a[0] + a[1] * z + a[2] * z * z)


def eq_response(bands: Sequence[Band], f: np.ndarray, fs: float = EQ_FS,
                precut: float = 0.0) -> np.ndarray:
    """The complex response of an EQ; PRECUT in dB (positive cuts)"""
    h = np.ones(len(f), complex) * 10 ** (-precut / 20)
    for kind, fc, q10, g10 in bands:
        if g10:
            h *= biquad_resp(*biquad(kind, fc, q10, g10, fs), f, fs)
    return h


def resp_db(bands: Sequence[Band], f: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.abs(eq_response(bands, f)))


@dataclass(frozen=True)
class Preset:
    enabled: bool
    precut: float               # dB
    bands: list[Band]


def read_cfg(path: Path) -> Preset:
    bands: list[Band | None] = [None] * 10
    precut, enabled = 0.0, False
    for line in path.read_text().splitlines():
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "eq enabled":
            enabled = v == "on"
        elif k == "eq precut":
            precut = int(v) / 10
        elif k.startswith("eq filter "):
            p = [s.strip() for s in v.split(",")]
            bands[int(k[10:])] = (p[3].upper(), int(p[0]), int(p[1]), int(p[2]))
    return Preset(enabled, precut, [b for b in bands if b])


def predicted(r: Result, cfg: Path, ch: str = "avg",
              sweep: str = "12") -> tuple[np.ndarray, np.ndarray]:
    """R's response with the preset CFG applied in the player's DSP"""
    g, y = fr_curve(r, ch, sweep)
    preset = read_cfg(cfg)
    e = 20 * np.log10(np.abs(eq_response(preset.bands, g, precut=preset.precut)))
    return g, norm_at(g, y + e)


# ---------------------------------------------------------------- fitting
# Parameters: per band log2(centre), ln(Q), gain dB; then the overall level.

def _bounds(max_boost: float, level: float) -> tuple[np.ndarray, np.ndarray]:
    """Gain -12..+MAX_BOOST dB; peaks Q 0.5..4 within 3/4 octave of their
    default centre; shelves Q 0.3..1"""
    lo: list[float] = []
    hi: list[float] = []
    for i, k in enumerate(KINDS):
        if k == LS:
            f, q = (20.0, 300.0), (0.3, 1.0)
        elif k == HS:
            f, q = (2000.0, 18000.0), (0.3, 1.0)
        else:
            n = NOMINAL[i]
            f, q = (max(25, n * 2 ** -0.75), min(16000, n * 2 ** 0.75)), (0.5, 4.0)
        lo += [np.log2(f[0]), np.log(q[0]), -12.0]
        hi += [np.log2(f[1]), np.log(q[1]), max_boost]
    return np.array([*lo, level]), np.array([*hi, level])


def _bands_of(p: np.ndarray, quantise: bool = False) -> list[Band]:
    """Parameters as bands; QUANTISE rounds to the .cfg's units"""
    out: list[Band] = []
    for i, k in enumerate(KINDS):
        f, q, g = 2 ** p[3 * i], np.exp(p[3 * i + 1]), p[3 * i + 2]
        if quantise:
            out.append((k, round(float(f)), round(float(q) * 10), round(float(g) * 10)))
        else:
            out.append((k, f, q * 10, g * 10))
    return out


def _fit(g: np.ndarray, target: np.ndarray, w: np.ndarray, max_boost: float, *,
         lam: float = 0.03, iters: int = 200, starts: int = 8) -> np.ndarray:
    """Projected Levenberg-Marquardt with bounds, from several deterministic
    starts; bands under WEAK_DB are then switched off and the rest refitted.
    The level is fixed at the target's median, so the bands correct
    deviations from the typical level instead of a broadband shift."""
    level = float(np.median(target[w > 0]))
    lo, hi = _bounds(max_boost, level)

    def res(p: np.ndarray) -> np.ndarray:
        e = resp_db(_bands_of(p), g) + p[-1]
        return np.concatenate([w * (e - target), lam * p[2:-1:3]])

    def lm(p: np.ndarray, active: np.ndarray) -> tuple[np.ndarray, float]:
        mu, r = 1e-2, res(p)
        for _ in range(iters):
            jac = np.zeros((len(r), len(p)))
            for j in np.flatnonzero(active):
                d = np.zeros(len(p))
                d[j] = 1e-4
                jac[:, j] = (res(p + d) - r) / 1e-4
            a = jac.T @ jac
            step = np.linalg.solve(a + mu * np.diag(np.diag(a) + 1e-9), -jac.T @ r)
            q = np.clip(p + step * active, lo, hi)
            rq = res(q)
            if rq @ rq < r @ r:
                done = r @ r - rq @ rq < 1e-9 * (r @ r) and mu < 1e-4
                p, r, mu = q, rq, mu * 0.3
                if done:
                    break
            else:
                mu *= 4
                if mu > 1e8:
                    break
        return p, float(r @ r)

    rng = np.random.default_rng(0)
    active = np.ones(len(lo), bool)
    active[-1] = False
    best: tuple[np.ndarray, float] | None = None
    for k in range(starts):
        p0: list[float] = []
        for i, kind in enumerate(KINDS):
            # shelves start where the target's ends are, at full size
            f = {LS: 50, HS: 8000}.get(kind, NOMINAL[i])
            q = 0.7 if kind != PK else 1.0
            ff = {LS: 25, HS: 16000}.get(kind, f)
            gain = (1.0 if kind != PK else 0.5) * (
                np.interp(np.log(ff), np.log(g), target) - level)
            if k:
                f *= 2 ** rng.uniform(-0.6, 0.6)
                q *= np.exp(rng.uniform(-0.5, 0.7))
            p0 += [np.log2(f), np.log(q), gain]
        cand = lm(np.clip(np.array([*p0, level]), lo, hi), active)
        if best is None or cand[1] < best[1]:
            best = cand
    if best is None:
        raise ValueError("no fit starts")
    p = best[0]
    weak = [i for i in range(10) if abs(p[3 * i + 2]) < WEAK_DB]
    if weak:
        for i in weak:
            p[3 * i + 2] = 0.0
            active[3 * i:3 * i + 3] = False
        p, _ = lm(p, active)
    return p


@dataclass(frozen=True)
class Fit:
    ref: str
    dev: str
    grid: np.ndarray
    target: np.ndarray          # reference minus device, dB
    weight: np.ndarray          # 1 inside the fitted band
    bands: list[Band]           # quantised
    precut: int                 # tenths of a dB
    peak: float                 # the preset's highest boost, dB
    fmin: float
    fmax: float

    @property
    def response(self) -> np.ndarray:
        return resp_db(self.bands, self.grid)

    @property
    def offset(self) -> float:
        """The overall level, which the fit leaves free"""
        return float(np.average(self.target - self.response, weights=self.weight))

    def _rms_max(self, y: np.ndarray) -> tuple[float, float]:
        y = y[self.weight > 0]
        return float(np.sqrt((y ** 2).mean())), float(np.abs(y).max())

    @property
    def before(self) -> tuple[float, float]:
        """The device's error (rms, max dB) without the EQ"""
        return self._rms_max(self.target - np.average(self.target, weights=self.weight))

    @property
    def residual(self) -> tuple[float, float]:
        """The error (rms, max dB) the preset is predicted to leave"""
        return self._rms_max(self.target - self.response - self.offset)

    def cfg(self) -> str:
        """The preset as a Rockbox .cfg; precut is the highest boost, so the
        EQ never raises a full-scale signal (the DSP clips at 16 bits before
        the software volume)"""
        lines = ["# headphone correction (arb audio fit-eq)",
                 f"# reference {self.ref}, device {self.dev}, {self.fmin:g}-{self.fmax:g} Hz, "
                 f"residual rms {self.residual[0]:.2f} dB",
                 "eq enabled: on", f"eq precut: {self.precut}"]
        for i, band in enumerate(self.bands):
            k, f, q, g = band
            if g == 0:          # off: the band's default centre and Q
                k, f, q = KINDS[i], NOMINAL[i], 7 if KINDS[i] != PK else 10
            lines.append(f"eq filter {i}: {f}, {q}, {g}, {k}")
        return "\n".join(lines) + "\n"


def fit_preset(ref: Result, dev: Result, *, channel: str = "avg", max_boost: float = 6.0,
               fmin: float = 25.0, fmax: float = 18000.0) -> Fit:
    """The preset that makes DEV's response (1/6-octave, normalised at 1 kHz)
    match REF's between FMIN and FMAX"""
    g, yr = fr_curve(ref, channel)
    _, yd = fr_curve(dev, channel)
    target = yr - yd
    w = ((g >= fmin) & (g <= fmax) & np.isfinite(target)).astype(float)
    target = np.nan_to_num(target)
    bands = _bands_of(_fit(g, target, w, max_boost), quantise=True)
    peak = float(resp_db(bands, log_grid(20, 22000, 96)).max())
    precut = max(0, int(np.ceil(peak * 10 - 1e-6)))
    return Fit(ref["label"], dev["label"], g, target, w, bands, precut, peak, fmin, fmax)
