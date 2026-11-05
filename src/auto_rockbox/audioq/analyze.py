"""One capture of the test signal: locate it, cut it into segments, measure.

Measures the frequency response and harmonic distortion (H2..H5) against
frequency from the sweeps (Farina deconvolution), THD+N against level at
1 kHz, the noise floor (flat and A-weighted), balance, polarity, crosstalk
and CCIF/SMPTE IMD. A result is a JSON-ready dict; NaN becomes None.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from .dsp import Spectrum, a_weight, arr, dbfs, log_grid, norm_at, smooth_mag, tone_metrics
from .signal import Segment, Timeline, synth_segment

GRID = log_grid(20, 20000, 48)
ANCHOR_Q = 0.8          # normalised correlation an anchor needs to count

Result = dict[str, Any]


def xcorr_peak(m: np.ndarray, tpl: np.ndarray, lo: int = 0,
               hi: int | None = None) -> tuple[int, float]:
    """Where in M[lo:hi] TPL matches best (overlap-save), and the correlation"""
    hi = len(m) if hi is None else min(hi, len(m))
    lo = max(lo, 0)
    n_tpl = len(tpl)
    n = 1 << int(np.ceil(np.log2(4 * n_tpl)))
    t = np.conj(np.fft.rfft(tpl, n))
    best, bi, step = 0.0, lo, n - n_tpl
    for b in range(lo, hi - n_tpl + 1, step):
        seg = m[b:b + n]
        # only starts inside [lo, hi - len(tpl)]: a block reaches well past hi
        c = np.fft.irfft(np.fft.rfft(seg, n) * t, n)[:min(step, len(seg) - n_tpl + 1,
                                                          hi - n_tpl + 1 - b)]
        if not len(c):
            break
        k = int(np.argmax(np.abs(c)))
        if abs(c[k]) > abs(best):
            best, bi = float(c[k]), b + k
    return bi, best


def anchor(m: np.ndarray, fs: float, s: Segment, guess: float,
           reach: float) -> tuple[int, float]:
    """Capture index of segment S near GUESS (+-REACH samples), and the
    normalised correlation there"""
    tpl = synth_segment(s, fs)
    i, c = xcorr_peak(m, tpl, int(guess - reach), int(guess + reach + len(tpl)))
    seg = m[i:i + len(tpl)]
    return i, abs(c) / (np.sqrt((tpl ** 2).sum() * (seg ** 2).sum()) + 1e-30)


@dataclass(frozen=True)
class Sync:
    i0: int                 # capture index of the start marker
    r: float                # one second of signal spans r * fs capture samples
    polarity: list[int]
    quality: float          # the worst anchor's normalised correlation
    anchors: int            # how many anchors the fit used


def locate(x: np.ndarray, fs: float, tl: Timeline) -> Sync:
    """The start chirp alone places the signal coarsely; then the chirps and
    sweeps (anchors) are each found again near where that predicts, and the
    start and clock ratio fitted to the ones that correlate well. An iPhone
    ramped its gain over the first ~15 s: its start chirp came out 88 ms
    late, the clock 590 ppm off, and every 15 s sweep smeared into a
    V-shaped response when only the two chirps were used."""
    s0 = tl["sync_start"]
    m = x.sum(1)
    c0_i, _ = xcorr_peak(m, synth_segment(s0, fs))
    pts = []
    for name in ["sync_start", *tl.named("sweep_"), "sync_end"]:
        s = tl[name]
        guess = c0_i + (s.start - s0.start) * fs
        i, q = anchor(m, fs, s, guess, 0.25 * fs)
        pts.append((s.start - s0.start, i, q))
    good = [(t, i) for t, i, q in pts if q >= ANCHOR_Q]
    if len(good) >= 2:
        t, i = np.array(good, float).T
        slope, i0 = np.polyfit(t, i, 1)
        r = slope / fs
        q = min(q for _, _, q in pts if q >= ANCHOR_Q)
    else:                                   # the chirps alone
        t_end, i_end, _ = pts[-1]
        i0, r = c0_i, (i_end - c0_i) / (t_end * fs)
        q = pts[0][2]
    # polarity from the cleaner chirp (the iPhone's ramped start chirp gave
    # +1, its end chirp -1)
    _, ip, _ = max((pts[0], pts[-1]), key=lambda p: p[2])
    tp = synth_segment(tl["sync_start" if ip == pts[0][1] else "sync_end"], fs)
    pol = [1 if np.dot(x[ip:ip + len(tp), c], tp) >= 0 else -1 for c in (0, 1)]
    return Sync(round(i0), float(r), pol, float(q), len(good))


class Capture:
    def __init__(self, x: np.ndarray, fs: float, tl: Timeline) -> None:
        self.x, self.fs, self.tl = x, fs, tl
        self.sync = locate(x, fs, tl)
        self.t_ref = tl["sync_start"].start
        self.fse = fs * self.sync.r          # the capture's rate in signal time

    def cut(self, name: str, pre: float = 0.0, post: float = 0.0,
            trim: float = 0.0) -> np.ndarray:
        s = self.tl[name]
        a = self.sync.i0 + (s.start - self.t_ref - pre + trim) * self.fse
        b = self.sync.i0 + (s.start - self.t_ref + s.dur + post - trim) * self.fse
        return self.x[round(a):round(b)]


def tones(cap: Capture, names: Sequence[str], trim: float = 0.3) -> list[dict[str, Any]]:
    out = []
    for n in names:
        s = cap.tl[n]
        y = cap.cut(n, trim=trim)
        ch = [tone_metrics(y[:, c], cap.fse, s.freq) for c in (0, 1)]
        out.append({"name": n, "freq": s.freq, "dbfs_in": s.dbfs, "ch": ch})
    return out


def sweep(cap: Capture, name: str, pre: float = 0.5, nharm: int = 5) -> dict[str, Any]:
    """Linear and harmonic responses (re the linear one) per output channel"""
    s = cap.tl[name]
    fse = cap.fse
    y = cap.cut(name, pre=pre, post=3.0)
    n = 1 << int(np.ceil(np.log2(len(y))))
    xs = np.zeros(n)
    tpl = synth_segment(s, fse)
    p = round(pre * fse)
    xs[p:p + len(tpl)] = tpl
    spec = np.fft.rfft(xs)
    f = np.fft.rfftfreq(n, 1 / fse)
    lo, hi = s.f1, min(s.f2, fse / 2 * 0.98)
    lf = np.log(f + 1e-9)

    def ramp(a: float, b: float) -> np.ndarray:
        return np.clip((lf - np.log(a)) / np.log(b / a), 0, 1)

    band = ramp(0.8 * lo, 1.1 * lo) * (1 - ramp(0.97 * hi, 1.03 * hi))
    band = 0.5 - 0.5 * np.cos(np.pi * band)
    inv = np.conj(spec) / (np.abs(spec) ** 2 + 1e-10 * np.abs(spec).max() ** 2) * band
    rate = s.dur / np.log(s.f2 / s.f1) * fse   # the sweep's rate constant, samples
    nw = 1 << int(np.ceil(np.log2(0.6 * fse)))
    fw = np.fft.rfftfreq(nw, 1 / fse)
    hs = [np.fft.irfft(np.fft.rfft(y[:, c], n) * inv, n) for c in (0, 1)]
    h = hs["LR".index(s.ch)]
    near = np.r_[np.arange(n - int(0.05 * fse), n), np.arange(int(0.1 * fse))]
    t0 = int(near[np.argmax(np.abs(h[near]))])
    t0 = t0 - n if t0 > n // 2 else t0

    def window(hh: np.ndarray, start: int, length: int, fin: float,
               fout: float) -> np.ndarray:
        idx = (np.arange(length) + start) % n
        w = np.ones(length)
        a, b = int(fin), int(fout)
        w[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / max(a, 1))
        w[length - b:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(b) / max(b, 1))
        return np.abs(np.fft.rfft(hh[idx] * w, nw)) ** 2

    res = {}
    for c in (0, 1):
        hh = hs[c]
        pre_n, lin_n = int(0.005 * fse), int(0.4 * fse)
        fr = smooth_mag(fw, window(hh, t0 - pre_n, pre_n + lin_n, pre_n, lin_n // 5), GRID, 6)
        v = 10 * np.log10(fr + 1e-30)
        v[(1.15 * lo > GRID) | (0.95 * hi < GRID)] = np.nan   # taper edges
        out = {"fr": v}
        for k in range(2, nharm + 1):
            # the k-th harmonic's response arrives L*ln(k) before the linear one
            tn = t0 - rate * np.log(k)
            a = 0.4 * rate * np.log((k + 1) / k)
            b = min(0.6 * rate * np.log(k / (k - 1)), 0.3 * fse)
            hn = smooth_mag(fw, window(hh, int(tn - a), int(a + b), a * 0.5, b * 0.5),
                            GRID * k, 6)
            v = 10 * np.log10(hn / (fr + 1e-30) + 1e-30)
            v[(GRID * k > hi * 0.93) | (1.15 * lo > GRID)] = np.nan
            out[f"hd{k}"] = v
        res["LR"[c]] = out
    return res


def noise(cap: Capture) -> list[dict[str, Any]]:
    y = cap.cut("noise", trim=1.0)
    out = []
    for c in (0, 1):
        sp = Spectrum(y[:, c], cap.fse)
        m = (sp.f > 20) & (sp.f < 1000)
        pk = sp.f[m][np.argsort(sp.p[m])[-3:][::-1]]
        out.append({"flat": dbfs(sp.band()), "aw": dbfs(sp.band(weight=a_weight)),
                    "hum": [round(float(f), 1) for f in pk]})
    return out


def imd(cap: Capture) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {"ccif_d2": [], "ccif_d3": [], "smpte": []}
    y = cap.cut("imd_ccif", trim=0.4)
    for c in (0, 1):
        sp = Spectrum(y[:, c], cap.fse)
        p = sp.power(sp.peak(19000)) + sp.power(sp.peak(20000))
        d2 = sp.power(1000)
        d3 = sp.power(18000) + sp.power(21000) if cap.fse / 2 > 21500 else np.nan
        out["ccif_d2"].append(float(10 * np.log10(d2 / p + 1e-30)))
        out["ccif_d3"].append(float(10 * np.log10(d3 / p + 1e-30)))
    y = cap.cut("imd_smpte", trim=0.4)
    for c in (0, 1):
        sp = Spectrum(y[:, c], cap.fse)
        f2 = sp.peak(7000, 0.01)
        f1 = sp.peak(60, 0.1)
        side = sum(sp.power(f2 + k * f1) for k in (-3, -2, -1, 1, 2, 3))
        out["smpte"].append(float(10 * np.log10(side / sp.power(f2) + 1e-30)))
    return out


def to_json(o: Any) -> Any:
    """Plain lists and numbers, rounded to 3 places, NaN as None"""
    if isinstance(o, dict):
        return {k: to_json(v) for k, v in o.items()}
    if isinstance(o, list | tuple | np.ndarray):
        return [to_json(v) for v in o]
    if isinstance(o, float | np.floating):
        return round(float(o), 3) if np.isfinite(o) else None
    if isinstance(o, np.integer):
        return int(o)
    return o


def analyse(x: np.ndarray, fs: int, tl: Timeline, label: str) -> Result:
    cap = Capture(x, fs, tl)
    r: Result = {"label": label, "fs": fs, "clock_ppm": (1 / cap.sync.r - 1) * 1e6,
                 "polarity": cap.sync.polarity, "sync_quality": cap.sync.quality,
                 "anchors": cap.sync.anchors, "grid": GRID}
    # the -20 dBFS step plays after any gain ramp at the start has settled
    lv = tones(cap, ["step_-20" if "step_-20" in tl else "level_1k"])[0]
    r["gain_1k"] = [c["level"] - lv["dbfs_in"] for c in lv["ch"]]
    r["balance"] = lv["ch"][0]["level"] - lv["ch"][1]["level"]
    r["noise"] = noise(cap)
    r["steps"] = tones(cap, tl.named("step_"))
    r["spots"] = tones(cap, tl.named("spot_"))
    r["imd"] = imd(cap)
    r["sweeps"] = {}
    for n in tl.named("sweep_"):
        _, lvl, ch = n.split("_")
        r["sweeps"].setdefault(lvl, {})[ch] = sweep(cap, n)
    return to_json(r)


def at(r: Result, curve: np.ndarray, f: float) -> float:
    return float(np.interp(np.log(f), np.log(r["grid"]), curve))


def report(r: Result) -> str:
    o = [f"# {r['label']}  (capture {r['fs']} Hz, player clock {r['clock_ppm']:+.0f} ppm "
         f"re capture, sync quality {r['sync_quality']:.2f} over {r['anchors']} anchors)",
         f"gain at 1 kHz: L {r['gain_1k'][0]:+.2f} dB  R {r['gain_1k'][1]:+.2f} dB  "
         f"(balance L-R {r['balance']:+.2f} dB); polarity L {r['polarity'][0]:+d} "
         f"R {r['polarity'][1]:+d}"]
    p = o.append
    for c, n in zip("LR", r["noise"], strict=True):
        p(f"noise {c}: {n['flat']:.1f} dBFS flat, {n['aw']:.1f} dBFS(A); "
          f"loudest <1 kHz: {n['hum']} Hz;  SNR(A) re 0 dBFS in: "
          f"{r['gain_1k']['LR'.index(c)] - n['aw']:.1f} dB")
    p("1 kHz steps (in dBFS: out level, THD+N dB/%, H2, H3)  [L | R]")
    for s in r["steps"]:
        p(f"  {s['dbfs_in']:4.0f}: " + " | ".join(
            f"{c['level']:7.2f} {c['thdn']:7.1f} {100 * 10 ** (c['thdn'] / 20):7.3f}%"
            f" {(c['h'] + [np.nan])[0]:6.1f} {(c['h'] + [np.nan, np.nan])[1]:6.1f}"
            for c in s["ch"]))
    lin = [s for s in r["steps"] if s["dbfs_in"] >= -20]
    ok = [s["dbfs_in"] for s in lin if max(c["thdn"] for c in s["ch"]) < -40]
    p(f"  max input with THD+N < 1%: {max(ok) if ok else 'none'} dBFS")
    m60 = [s for s in r["steps"] if s["dbfs_in"] == -60]
    if m60:
        p(f"  dynamic range (AES17 -60 dBFS method): "
          f"{60 - max(c['thdn'] for c in m60[0]['ch']):.1f} dB")
    p("spot tones -6 dBFS (Hz: gain dB, THD+N dB)  [L | R]")
    for s in r["spots"]:
        p(f"  {s['freq']:6.0f}: " + " | ".join(
            f"{c['level'] - s['dbfs_in']:+6.2f} {c['thdn']:7.1f}" for c in s["ch"]))
    i = r["imd"]
    p(f"IMD CCIF 19+20k: d2 {i['ccif_d2'][0]:.1f}/{i['ccif_d2'][1]:.1f} dB, d3 "
      f"{i['ccif_d3'][0]}/{i['ccif_d3'][1]} dB; SMPTE 60+7k: "
      f"{i['smpte'][0]:.1f}/{i['smpte'][1]:.1f} dB")
    for lvl, sw in r["sweeps"].items():
        for c in "LR":
            fr = arr(sw[c][c]["fr"])
            xt = arr(sw[c]["LR"[1 - "LR".index(c)]]["fr"]) - fr
            g1k = at(r, fr, 1000)
            pts = " ".join(f"{f}:{at(r, fr, f) - g1k:+.1f}" for f in
                           (20, 50, 100, 5000, 10000, 15000, 20000))
            hd = " ".join(f"H{n} {np.nanmax(arr(sw[c][c][f'hd{n}'])):.0f}" for n in (2, 3))
            p(f"sweep -{lvl} {c}: FR re 1k {pts}; worst {hd} dB; crosstalk "
              f"1k {at(r, xt, 1000):.0f} 10k {at(r, xt, 10000):.0f} dB")
    return "\n".join(o)


def volsteps(x: np.ndarray, fs: int, label: str, *, volumes: Sequence[float] | None = None,
             min_s: float = 1.5, tol: float = 0.2) -> Result:
    """THD+N per volume step of a steady 1 kHz tone (signal.VOLSTEPS): its
    level plateaus, found in 100 ms blocks, labelled with VOLUMES in order"""
    n = int(fs * 0.1)
    blk = x[:len(x) // n * n, 0].reshape(-1, n)
    lv = 10 * np.log10((blk ** 2).mean(1) * 2 + 1e-30)
    steps: list[dict[str, Any]] = []
    start = 0
    for i in range(1, len(lv) + 1):
        if i == len(lv) or abs(lv[i] - np.median(lv[start:i])) > tol:
            if (i - start) * 0.1 >= min_s and np.median(lv[start:i]) > -80:
                y = x[(start + 3) * n:(i - 3) * n]
                ch = [tone_metrics(y[:, c], fs, 1000.0) for c in (0, 1)]
                steps.append({"start": start * 0.1, "dur": (i - start) * 0.1, "ch": ch})
            start = i
    for s, v in zip(steps, volumes or (), strict=False):
        s["volume"] = v
    return to_json({"label": label, "fs": fs, "steps": steps})


def fr_curve(r: Result, ch: str = "avg", sweep: str = "12") -> tuple[np.ndarray, np.ndarray]:
    """A result's response at the -SWEEP dBFS sweep, normalised at 1 kHz: one
    channel, or "avg" the power mean of both"""
    sw = r["sweeps"][sweep]
    if ch == "avg":
        y = 10 * np.log10((10 ** (arr(sw["L"]["L"]["fr"]) / 10) +
                           10 ** (arr(sw["R"]["R"]["fr"]) / 10)) / 2)
    else:
        y = arr(sw[ch][ch]["fr"])
    g = np.array(r["grid"])
    return g, norm_at(g, y)
