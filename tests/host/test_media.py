"""Test media: step-down detection, the OKI ADPCM encoder and spectrum
classification (the last with ffmpeg, if present)."""

from __future__ import annotations

import shutil
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from auto_rockbox import linein, media

RATE = media.RATE


def tone(left: float, right: float, seconds: float, db: float = -10.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    amp = 10 ** (db / 20)
    return amp * np.stack([np.sin(2 * np.pi * left * t), np.sin(2 * np.pi * right * t)], axis=1)


def int16(a: np.ndarray) -> np.ndarray:
    return (a * 32767).astype(np.int16)


def write_wav(path: Path, a: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(a.shape[1])
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(int16(a).tobytes())
    return path


def oki_decode(data: bytes) -> np.ndarray:
    """The decoder side of media.oki_adpcm, to check the encoder against"""
    pred, idx, out = 0, 0, []
    for byte in data:
        for code in (byte >> 4, byte & 15):
            step = media.OKI_STEPS[idx]
            delta = step >> 3
            for bit, part in ((4, step), (2, step >> 1), (1, step >> 2)):
                if code & bit:
                    delta += part
            pred = max(-2048, min(2047, pred - delta if code & 8 else pred + delta))
            idx = max(0, min(48, idx + media.OKI_ADJUST[code & 7]))
            out.append(pred << 4)
    return np.array(out, float) / 32768


class TestAnalysis(unittest.TestCase):
    def test_step_down(self) -> None:
        stepped = np.concatenate([tone(1000, 1500, 3), tone(1000, 1500, 2, -22),
                                  np.zeros((RATE, 2))])
        assert media.step_down(int16(stepped)) == 3.0
        assert media.step_down(int16(tone(1000, 1500, 4))) is None
        to_silence = np.concatenate([tone(1000, 1500, 3), np.zeros((2 * RATE, 2))])
        assert media.step_down(int16(to_silence)) is None

    def test_oki_adpcm(self) -> None:
        x = int16(tone(1000, 1000, 1)[:, 0])[::6]           # 8 kHz
        data = media.oki_adpcm(x)
        assert len(data) == len(x) // 2
        y = oki_decode(data)
        assert abs(linein.dominant_hz(y, media.VOX_RATE) - 1000) < 2
        assert np.corrcoef(y[800:], x[800:])[0, 1] > 0.98

    @unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg")
    def test_classify(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            assert media.classify(write_wav(root / "s.wav", tone(1000, 1500, 5))) == "stereo"
            assert media.classify(write_wav(root / "m.wav", tone(1000, 1000, 5))) == "tone"
            assert media.classify(write_wav(root / "a.wav", tone(440, 660, 5))) == "music"
            (root / "x.vox").write_bytes(bytes(range(256)) * 64)
            (root / "x.bin").write_bytes(bytes(range(256)) * 64)
            assert media.classify(root / "x.vox") == "tone"
            assert media.classify(root / "x.bin") == "unknown"
            e = media.describe(root, write_wav(
                root / "t.wav", np.concatenate([tone(1000, 1500, 4), tone(1000, 1500, 2, -22)])))
            assert e == media.Entry("t.wav", "stereo", 6.0, 4.0), e


if __name__ == "__main__":
    unittest.main()
