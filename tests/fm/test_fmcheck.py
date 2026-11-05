"""fmcheck on synthetic recordings: run with `.venv/bin/python -m tests.fm.test_fmcheck`."""

import tempfile
import wave
from pathlib import Path

import numpy as np

from auto_rockbox import fmcheck

RATE = 44100


def write(path: Path, x: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.clip(x * 32767, -32768, 32767).astype("<i2").tobytes())


def music(seconds: float, stereo: float) -> np.ndarray:
    """Band-limited noise at about -20 dBFS; STEREO 0 = mono, 1 = independent"""
    rng = np.random.default_rng(1)
    n = int(seconds * RATE)
    k = np.ones(64) / 64                       # a crude low-pass: no big steps
    common = np.convolve(rng.standard_normal(n), k, "same")
    own = [np.convolve(rng.standard_normal(n), k, "same") for _ in range(2)]
    x = np.stack([(1 - stereo) * common + stereo * o for o in own], axis=1)
    return 0.1 * x / np.abs(x).max()


def test() -> None:
    with tempfile.TemporaryDirectory() as d:
        clean, wrapped, mono = Path(d, "clean.wav"), Path(d, "wrap.wav"), Path(d, "mono.wav")
        x = music(5, 0.3)
        write(clean, x)
        y = x.copy()
        for t in (1.0, 3.5):                   # the YP3's: +37k in one sample, then decay
            i = int(t * RATE)
            y[i:i + 4000, 0] += 37000 / 32768 * np.exp(-np.arange(4000) / 800)
        write(wrapped, np.clip(y, -1, 1))
        write(mono, music(5, 0.0))
        c, w, m = fmcheck.check(clean), fmcheck.check(wrapped), fmcheck.check(mono)
    assert c.wraps == 0, c
    assert w.wraps == 2 and w.wrap_times == [1.0, 3.5], w
    assert c.correlation is not None and 0.5 < c.correlation < 0.99, c
    assert m.correlation is not None and m.correlation > 0.999, m
    assert m.side_db is not None and m.side_db < -60, m
    print("ok", c, w, m, sep="\n")


if __name__ == "__main__":
    test()
