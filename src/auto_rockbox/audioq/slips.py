"""Sample slips in a steady tone: phase steps of the tone over 10 ms.

A DAC that misses a sample period delays everything after it by one sample:
the tone's phase steps by 360 * F0 / (the player's rate) degrees, heard as a
click. The level hardly moves, so a level or THD scan misses most of them.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .signal import GEN_FS

SPAN = 10               # ms over which the phase step is measured
EDGE = 30               # ms skipped at the tone's start and stop


@dataclass(frozen=True)
class Slips:
    start: float                        # the steady stretch, s into the recording
    seconds: float
    hz: float                           # the tone as captured
    events: list[tuple[float, float]]   # (s into the recording, slip in samples)

    @property
    def net(self) -> float:
        return sum(v for _, v in self.events)


def find(y: np.ndarray, fs: int, f0: float = 1000.0, player_fs: int = GEN_FS) -> Slips:
    """Slips in Y (one channel) of a tone the player made at F0 Hz, playing
    at PLAYER_FS: the tone is mixed to 0 Hz with its measured frequency, the
    phase taken per 1 ms, and phase steps over SPAN ms of more than 0.4 of a
    sample period counted"""
    n = np.arange(len(y))
    sp = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    k = int(np.argmax(sp))
    f = (k + 0.5 * (sp[k - 1] - sp[k + 1]) / (sp[k - 1] - 2 * sp[k] + sp[k + 1])) * fs / len(y)
    blk = fs // 1000
    z = y * np.exp(-2j * np.pi * f * n / fs)
    nb = len(z) // blk
    zb = z[:nb * blk].reshape(nb, blk).mean(1)
    amp = np.abs(zb)
    on = np.flatnonzero(amp > np.median(amp) * 0.5)
    a, b = int(on[0]) + EDGE, int(on[-1]) - EDGE
    ph = np.degrees(np.unwrap(np.angle(zb[a:b])))
    d = ph[SPAN:] - ph[:-SPAN]
    d -= np.median(d)
    per_sample = 360 * f0 / player_fs
    events: list[tuple[float, float]] = []
    i = 0
    while i < len(d):
        if abs(d[i]) > 0.4 * per_sample:
            j = i + int(np.argmax(np.abs(d[i:i + 2 * SPAN])))
            events.append(((a + j + SPAN / 2) * blk / fs, float(d[j] / per_sample)))
            i = j + 2 * SPAN
        else:
            i += 1
    return Slips(a * blk / fs, (b - a) * blk / fs, float(f), events)
