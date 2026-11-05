"""Checks of an FM recording (a WAV the player recorded from its tuner, or
the Line In capture of its FM monitor) for the faults FM bring-up hits:

- **ADC overload that wraps**: some audio ADCs (the YP3's aux input) do
  not clip flat when overdriven. The sample jumps by most of full scale in
  one step and then decays exponentially, as if the overload came before a
  DC-removing high-pass. Each one sounds like a loud low thump. Real audio
  never steps that far between neighbouring samples.
- **Level**: peak and RMS in dBFS, to set the capture gain with headroom.
- **Stereo**: the L/R correlation and the side level against mid. A mono
  programme, or a tuner forced to mono, reads correlation ~1.000 and side
  < -35 dB. Real stereo music reads ~0.9 and side -10..-20 dB.
"""

from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

WRAP_STEP = 0.5             # of full scale (32768), between neighbouring samples
SETTLE_S = 0.002            # a wrap stays off for this long; loud treble swings back


@dataclass
class FmCheck:
    seconds: float
    peak_dbfs: float
    rms_dbfs: float
    wraps: int                          # overload wraps, both channels
    wrap_times: list[float]             # seconds into the file (first few)
    correlation: float | None           # None for mono files
    side_db: float | None               # side level against mid


def _db(x: float) -> float:
    return 20 * float(np.log10(max(x, 1e-12)))


def check(path: Path, step: float = WRAP_STEP) -> FmCheck:
    with wave.open(str(path)) as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: 16-bit PCM WAV needed")
        rate, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, ch)
    x = a.astype(np.float64) / 32768.0
    n = max(1, int(rate * SETTLE_S))
    at = []
    for i, c in zip(*np.nonzero(np.abs(np.diff(x, axis=0)) > step), strict=True):
        before = x[max(0, i - n + 1):i + 1, c].mean()
        after = x[i + 1:i + 1 + n, c].mean()
        if abs(after - before) > step / 2:
            at.append(i)
    at = np.unique(np.array(at, dtype=np.int64))
    # one overload can step on both channels and on neighbouring samples
    starts = at[np.concatenate(([True], np.diff(at) > rate // 100))] if at.size else at
    corr = side = None
    if ch == 2:
        left, right = x[:, 0] - x[:, 0].mean(), x[:, 1] - x[:, 1].mean()
        denom = float(np.sqrt((left**2).sum() * (right**2).sum()))
        corr = float((left * right).sum() / denom) if denom else None
        mid, sid = (left + right) / 2, (left - right) / 2
        side = _db(float(np.sqrt((sid**2).mean()))) - _db(float(np.sqrt((mid**2).mean())))
    return FmCheck(len(x) / rate, _db(float(np.abs(x).max())),
                   _db(float(np.sqrt((x**2).mean()))), int(starts.size),
                   [round(float(s) / rate, 3) for s in starts[:10]], corr, side)
