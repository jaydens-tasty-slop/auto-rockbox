"""A received stream's analysis on made-up WAV files: the tone, a dropout on
one channel, and silence at the very ends that is not one."""

from __future__ import annotations

import tempfile
import wave
from pathlib import Path

import numpy as np

from auto_rockbox.btpeer.analysis import analyse_wav

RATE = 44100


def _wav(path: Path, left: np.ndarray, right: np.ndarray) -> Path:
    pcm = (np.stack([left, right], axis=1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return path


def _tone(seconds: float, hz: float = 1000.0) -> np.ndarray:
    return 0.5 * np.sin(2 * np.pi * hz * np.arange(int(seconds * RATE)) / RATE)


def test_clean_tone() -> None:
    lines: list[str] = []
    with tempfile.TemporaryDirectory() as tmp:
        r = analyse_wav(_wav(Path(tmp) / "a.wav", _tone(3), _tone(3, 500)), lines.append)
    assert r.rate == RATE and r.channels == 2 and abs(r.seconds - 3) < 1e-9
    assert [round(m) for m in r.medians if m] == [1000, 500]
    assert r.gaps == [] and r.edges == []
    assert len(lines) == 1 + 3 + 2          # header, a line a second, a median per channel


def test_dropout_on_one_channel() -> None:
    left, right = _tone(3), _tone(3)
    left[int(1.5 * RATE):int(1.52 * RATE)] = 0
    left[:2 * 44] = 0                       # 2 ms at the start: an edge, not a gap
    with tempfile.TemporaryDirectory() as tmp:
        r = analyse_wav(_wav(Path(tmp) / "a.wav", left, right), lambda _s: None)
    assert len(r.gaps) == 1
    start, ms = r.gaps[0]
    assert abs(start - 1.5) < 0.002 and 18 <= ms <= 22
    assert r.edges == [(0, 2)]


def test_silence() -> None:
    quiet = np.zeros(2 * RATE)
    with tempfile.TemporaryDirectory() as tmp:
        r = analyse_wav(_wav(Path(tmp) / "a.wav", quiet, quiet), lambda _s: None)
    assert r.medians == [None, None]
