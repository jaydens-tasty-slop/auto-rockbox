"""The test signal both devices play (~2.6 min, 44.1 kHz stereo).

Timeline: a 2 s lead (a player's fade-in or pop), up-chirp sync marker,
1 kHz at -20 dBFS, 8 s of digital silence, 1 kHz steps -60..0 dBFS,
exponential sweeps 20 Hz-20 kHz (-12 dBFS L then R, -3 dBFS L then R), spot
tones at -6 dBFS, CCIF 19+20 kHz and SMPTE 60 Hz + 7 kHz (4:1) IMD, and a
down-chirp end marker. The chirps and sweeps locate every segment and the
clock offset between player and capture, so no timestamps are needed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .audiofile import flac_encode, require_flac, write_wav

GEN_FS = 44100
SYNC_F1, SYNC_F2, SYNC_DUR, SYNC_DBFS = 300.0, 6000.0, 1.0, -10.0
STEP_LEVELS = (-60, -40, -20, -10, -6, -3, -1, 0)   # ascending: gentle on a clipping output
SPOT_FREQS = (50, 100, 1000, 6000, 10000, 15000)
SWEEP_F1, SWEEP_F2, SWEEP_DUR, SWEEP_TAIL = 20.0, 20000.0, 15.0, 3.0
SIGNAL_NAME = "test_signal"


@dataclass(frozen=True)
class Segment:
    name: str
    kind: str               # chirp, tone, silence, sweep or twotone
    start: float            # seconds into the signal
    dur: float
    ch: str = "LR"          # the channels it plays on
    dbfs: float = 0.0       # peak level
    freq: float = 0.0       # tone
    f1: float = 0.0         # chirp and sweep start; twotone's first tone
    f2: float = 0.0
    a1: float = 0.0         # twotone's amplitudes
    a2: float = 0.0


# The volume-step experiment's signal: a steady 1 kHz tone, stepped on the player
VOLSTEPS = Segment("volsteps", "tone", 0.0, 120.0, freq=1000.0, dbfs=-3.0)


@dataclass(frozen=True)
class Timeline:
    segments: tuple[Segment, ...]
    total: float

    def __getitem__(self, name: str) -> Segment:
        return next(s for s in self.segments if s.name == name)

    def __contains__(self, name: str) -> bool:
        return any(s.name == name for s in self.segments)

    def named(self, prefix: str) -> list[str]:
        return [s.name for s in self.segments if s.name.startswith(prefix)]

    def as_json(self) -> dict[str, Any]:
        return {"fs": GEN_FS, "total": self.total, "segments": [asdict(s) for s in self.segments]}


def timeline() -> Timeline:
    segs: list[Segment] = []
    t = 2.0

    def add(name: str, kind: str, dur: float, gap: float = 0.5, **kw: Any) -> None:
        nonlocal t
        segs.append(Segment(name, kind, round(t, 6), dur, **kw))
        t += dur + gap

    add("sync_start", "chirp", SYNC_DUR, 1.0, f1=SYNC_F1, f2=SYNC_F2, dbfs=SYNC_DBFS)
    add("level_1k", "tone", 5.0, 1.0, freq=1000.0, dbfs=-20.0)
    add("noise", "silence", 8.0, 0.0)
    t += 1.0
    for lv in STEP_LEVELS:
        add(f"step_{lv}", "tone", 3.0, freq=1000.0, dbfs=float(lv))
    t += 0.5
    for lv in (-12, -3):
        for ch in "LR":
            add(f"sweep_{-lv}_{ch}", "sweep", SWEEP_DUR, SWEEP_TAIL + 0.5, ch=ch,
                f1=SWEEP_F1, f2=SWEEP_F2, dbfs=float(lv))
    for f in SPOT_FREQS:
        add(f"spot_{f}", "tone", 3.0, freq=float(f), dbfs=-6.0)
    add("imd_ccif", "twotone", 4.0, f1=19000.0, f2=20000.0, a1=0.5, a2=0.5, dbfs=-6.0)
    add("imd_smpte", "twotone", 4.0, 1.0, f1=60.0, f2=7000.0, a1=0.8, a2=0.2, dbfs=-6.0)
    add("sync_end", "chirp", SYNC_DUR, 1.0, f1=SYNC_F2, f2=SYNC_F1, dbfs=SYNC_DBFS)
    return Timeline(tuple(segs), round(t, 6))


def load_timeline(path: Path) -> Timeline:
    """The timeline saved beside a generated signal; without one, this
    version's (a signal made by an older version may differ)"""
    if not path.exists():
        return timeline()
    d = json.loads(path.read_text())
    return Timeline(tuple(Segment(**s) for s in d["segments"]), d["total"])


def fade(n: int, fs: float, t_in: float, t_out: float) -> np.ndarray:
    w = np.ones(n)
    a, b = int(t_in * fs), int(t_out * fs)
    if a:
        w[:a] = 0.5 - 0.5 * np.cos(np.pi * np.arange(a) / a)
    if b:
        w[n - b:] = 0.5 + 0.5 * np.cos(np.pi * np.arange(b) / b)
    return w


def ess(f1: float, f2: float, dur: float, fs: float) -> tuple[np.ndarray, float]:
    """Exponential sine sweep (Farina), unit amplitude, no fades, and its
    rate constant L (seconds per e-fold of frequency)"""
    t = np.arange(round(dur * fs)) / fs
    rate = dur / np.log(f2 / f1)
    return np.sin(2 * np.pi * f1 * rate * (np.exp(t / rate) - 1)), rate


def synth_segment(s: Segment, fs: float) -> np.ndarray:
    """One segment, mono, in full-scale units. FS need not be an integer:
    the analysis synthesises templates at the capture's corrected rate."""
    n = round(s.dur * fs)
    t = np.arange(n) / fs
    amp = 10 ** (s.dbfs / 20)
    if s.kind == "silence":
        return np.zeros(n)
    if s.kind == "tone":
        return amp * np.sin(2 * np.pi * s.freq * t) * fade(n, fs, .01, .01)
    if s.kind == "twotone":
        x = s.a1 * np.sin(2 * np.pi * s.f1 * t) + s.a2 * np.sin(2 * np.pi * s.f2 * t)
        return amp * x * fade(n, fs, .02, .02)
    if s.kind in ("chirp", "sweep"):
        x, _ = ess(s.f1, s.f2, s.dur, fs)
        fi, fo = (0.05, 0.005) if s.kind == "sweep" else (0.01, 0.01)
        if s.f1 > s.f2:
            fi, fo = fo, fi
        return amp * x * fade(n, fs, fi, fo)
    raise ValueError(f"segment kind {s.kind}")


def synth(fs: int, tl: Timeline | None = None) -> np.ndarray:
    """The whole signal, float frames (n, 2)"""
    tl = tl or timeline()
    out = np.zeros((round(tl.total * fs), 2))
    for s in tl.segments:
        x = synth_segment(s, fs)
        i = round(s.start * fs)
        for c, name in enumerate("LR"):
            if name in s.ch:
                out[i:i + len(x), c] += x
    return out


def write_signal(out: Path) -> Timeline:
    """OUT/test_signal_16.flac and _24.flac (the reference device: lossless
    and native on most phones), _16.wav, test_signal.json (the timeline the
    analysis reads) and volsteps_16.wav/.flac (VOLSTEPS)"""
    require_flac()                          # before writing anything
    out.mkdir(parents=True, exist_ok=True)
    tl = timeline()
    x = synth(GEN_FS, tl)
    base = out / SIGNAL_NAME
    for bits in (16, 24):
        wav = out / f"{SIGNAL_NAME}_{bits}.wav"
        write_wav(wav, x, GEN_FS, bits)
        flac_encode(wav, wav.with_suffix(".flac"), keep_wav=bits == 16)
    base.with_suffix(".json").write_text(json.dumps(tl.as_json(), indent=1) + "\n")
    v = synth_segment(VOLSTEPS, GEN_FS)
    wav = out / "volsteps_16.wav"
    write_wav(wav, np.stack([v, v], 1), GEN_FS, 16)
    flac_encode(wav, wav.with_suffix(".flac"))
    return tl
