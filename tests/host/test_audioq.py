"""Audio quality: the pieces that need no full analysis run (the end-to-end
check is `arb audio selftest`)."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from auto_rockbox import linein
from auto_rockbox.audioq import audiofile, dsp, eq, signal, slips

CFG = """# a preset
eq enabled: on
eq precut: 30
eq filter 0: 50, 7, 30, LOW_SHELF
eq filter 1: 64, 10, 0, PEAK
eq filter 5: 1000, 20, -60, PEAK
eq filter 9: 8000, 7, 0, HIGH_SHELF
"""


class TestEq(unittest.TestCase):
    def test_cfg_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.cfg"
            p.write_text(CFG)
            preset = eq.read_cfg(p)
        assert preset.enabled
        assert preset.precut == 3.0
        assert preset.bands[0] == (eq.LS, 50, 7, 30)
        assert preset.bands[2] == (eq.PK, 1000, 20, -60)
        assert len(preset.bands) == 4

    def test_peak_gain_at_centre(self) -> None:
        h = eq.eq_response([(eq.PK, 1000, 20, -60)], np.array([1000.0, 20.0, 15000.0]))
        db = 20 * np.log10(np.abs(h))
        assert abs(db[0] + 6.0) < 0.01
        assert abs(db[1]) < 0.05 and abs(db[2]) < 0.05

    def test_shelves_and_precut(self) -> None:
        f = np.array([10.0, 20000.0])
        low = 20 * np.log10(np.abs(eq.eq_response([(eq.LS, 100, 7, 60)], f)))
        high = 20 * np.log10(np.abs(eq.eq_response([(eq.HS, 2000, 7, -40)], f, precut=1.0)))
        assert abs(low[0] - 6.0) < 0.1 and abs(low[1]) < 0.05
        assert abs(high[0] + 1.0) < 0.05 and abs(high[1] + 5.0) < 0.2

    def test_band_off_at_zero_gain(self) -> None:
        h = eq.eq_response([(eq.PK, 1000, 10, 0)], np.array([1000.0]))
        assert abs(h[0] - 1) < 1e-12


class TestSignal(unittest.TestCase):
    def test_timeline(self) -> None:
        tl = signal.timeline()
        assert tl.total == 154.0
        assert len(tl.segments) == 24
        assert tl.named("sweep_") == ["sweep_12_L", "sweep_12_R", "sweep_3_L", "sweep_3_R"]
        assert tl["sync_end"].f1 > tl["sync_end"].f2          # the end marker chirps down

    def test_timeline_json_round_trip(self) -> None:
        tl = signal.timeline()
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.json"
            p.write_text(json.dumps(tl.as_json()))
            assert signal.load_timeline(p) == tl
            assert signal.load_timeline(Path(d) / "missing.json") == tl

    def test_wav_round_trip(self) -> None:
        x = np.stack([np.linspace(-0.9, 0.9, 1000), np.zeros(1000)], 1)
        with tempfile.TemporaryDirectory() as d:
            for bits in (16, 24):
                p = Path(d) / f"x{bits}.wav"
                audiofile.write_wav(p, x, 44100, bits)
                y, fs = audiofile.read_audio(p)
                assert fs == 44100 and y.shape == x.shape
                # scale (q-1)/q, TPDF dither and rounding: under 2.5 LSB
                assert np.abs(y - x).max() < 2.5 / 2 ** (bits - 1)
                assert not y[:, 1].any()            # digital silence stays silent


class TestDsp(unittest.TestCase):
    def test_tone_metrics(self) -> None:
        fs = 48000
        t = np.arange(fs) / fs
        x = 0.5 * np.sin(2 * np.pi * 1000 * t) + 0.005 * np.sin(2 * np.pi * 2000 * t)
        m = dsp.tone_metrics(x, fs, 1000.0)
        assert abs(m["level"] + 6.02) < 0.05
        assert abs(m["h"][0] + 40.0) < 0.1                 # H2 at 1% of the fundamental
        assert abs(m["thdn"] + 40.0) < 0.2


class TestSlips(unittest.TestCase):
    def test_one_sample_slip(self) -> None:
        """A tone delayed by one 44.1 kHz sample after 2 s, as a DAC that
        missed a period plays it"""
        fs = 48000
        t = np.arange(4 * fs) / fs
        t = np.where(t >= 2.0, t - 1 / 44100, t)
        s = slips.find(0.3 * np.sin(2 * np.pi * 1000 * t), fs)
        assert len(s.events) == 1
        at, size = s.events[0]
        assert abs(at - 2.0) < 0.02 and abs(size + 1.0) < 0.1

    def test_steady_tone(self) -> None:
        fs = 48000
        t = np.arange(3 * fs) / fs
        assert slips.find(0.3 * np.sin(2 * np.pi * 997 * t), fs).events == []


class TestCapture(unittest.TestCase):
    def test_seconds_concatenate_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            pub = linein.Publisher(Path(d), 10, "test", None)
            for seq, value in ((7, 100), (8, 200)):
                raw = np.full((linein.RATE, 2), value, "<i2").tobytes()
                pub.publish(seq, raw, datetime(2026, 11, 5, 12, 0, seq, tzinfo=UTC), seq)
            x, fs = audiofile.load_capture(linein.history(Path(d)))
        assert fs == linein.RATE and x.shape == (2 * linein.RATE, 2)
        assert x[0, 0] == 100 / 32768 and x[-1, 1] == 200 / 32768

    def test_empty_window(self) -> None:
        with self.assertRaises(audiofile.AudioError):
            audiofile.load_capture([])


if __name__ == "__main__":
    unittest.main()
