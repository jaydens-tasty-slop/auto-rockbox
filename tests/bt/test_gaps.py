"""Zero runs in the player's stream: the tracker across blocks, the WAV scan,
and the ours-walk recording against the numbers the old gaps.py gave."""

from __future__ import annotations

import tempfile
import unittest
import wave
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from auto_rockbox.bt import gaps, rundir

from .fixtures import XM3, need

RATE = 44100
T0 = datetime.fromisoformat("2026-10-28T21:45:28.000-06:00")


def tone(frames: int) -> np.ndarray:
    t = np.arange(frames)
    x = (8000 * np.sin(2 * np.pi * 1000 * t / RATE)).astype(np.int16)
    x[x == 0] = 1                       # a tone's own zero crossings are no gap
    return np.stack([x, x], axis=1)


class Lines:
    def __init__(self) -> None:
        self.lines: list[tuple[str, datetime]] = []

    def __call__(self, text: str, at: datetime) -> None:
        self.lines.append((text, at))

    def words(self, word: str) -> list[tuple[str, datetime]]:
        return [x for x in self.lines if x[0].startswith(word)]


class TrackerTest(unittest.TestCase):
    def test_run_across_blocks(self) -> None:
        out = Lines()
        t = gaps.Tracker(out, RATE, 10)
        a = tone(RATE)
        a[-100:] = 0
        b = tone(RATE)
        b[:341] = 0                     # 441 frames in all: 10 ms
        t.feed(a, T0)
        t.feed(b, T0 + timedelta(seconds=1))
        assert t.gaps == [(T0 + timedelta(seconds=(RATE - 100) / RATE), 10)], t.gaps
        assert [x for x, _ in out.words("gap")] == ["gap 10"]
        assert [x[0].split()[:2] for x in out.words("level")] == [["level", "0"],
                                                                   ["level", "1"]]

    def test_short_run_and_open_end(self) -> None:
        out = Lines()
        t = gaps.Tracker(out, RATE, 10)
        a = tone(RATE)
        a[1000:1400] = 0                # 9 ms: under the limit
        a[-882:] = 0                    # 20 ms, still open when the stream stops
        t.feed(a, T0)
        assert t.gaps == []
        t.close()
        assert t.gaps == [(T0 + timedelta(seconds=(RATE - 882) / RATE), 20)]
        assert "1 gaps >= 10 ms, 0.0 s silent" in t.summary()

    def test_whole_block_silent(self) -> None:
        t = gaps.Tracker(Lines(), RATE, 10)
        for i in range(3):
            t.feed(np.zeros((RATE, 2), np.int16), T0 + timedelta(seconds=i))
        t.feed(tone(RATE), T0 + timedelta(seconds=3))
        assert t.gaps == [(T0, 3000)]

    def test_one_channel_silent_is_no_gap(self) -> None:
        t = gaps.Tracker(Lines(), RATE, 10)
        a = tone(RATE)
        a[:22050, 0] = 0
        t.feed(a, T0)
        t.close()
        assert t.gaps == []


class WavTest(unittest.TestCase):
    def write(self, path: Path, a: np.ndarray, width: int = 2) -> None:
        with path.open("wb") as f, wave.open(f, "wb") as w:
            w.setnchannels(2)
            w.setsampwidth(width)
            w.setframerate(RATE)
            w.writeframes(a.tobytes() if width == 2 else bytes(len(a) * 2))

    def test_scan(self) -> None:
        a = tone(3 * RATE)
        a[int(1.5 * RATE):int(1.55 * RATE)] = 0
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.wav"
            self.write(p, a)
            t = gaps.scan_wav(p, T0, Lines(), 10)
        assert t.gaps == [(T0 + timedelta(seconds=1.5), 50)]
        assert t.seconds == 3 and t.frames == 3 * RATE

    def test_not_16_bit(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.wav"
            self.write(p, tone(100), width=1)
            with self.assertRaises(gaps.GapError):
                gaps.scan_wav(p, T0, Lines())

    def test_header_round_trip(self) -> None:
        assert gaps.logged_min_ms("# " + gaps.header(50) + "\n") == 50


class FixtureTest(unittest.TestCase):
    def test_ours_walk(self) -> None:
        """build/xm3/ours-walk/gaps.txt (the old gaps.py): 177.9 s recorded,
        50 gaps >= 10 ms, 89218 ms silent, the drop's gap 56232 ms"""
        run = need(XM3 / "ours-walk")
        start = datetime.fromisoformat((run / "started.txt").read_text().strip())
        with tempfile.TemporaryDirectory() as d, rundir.Log(Path(d) / "g.txt", None) as log:
            t = gaps.scan_wav(run / "stream.wav", start, log)
        assert len(t.gaps) == 50
        assert sum(ms for _, ms in t.gaps) == 89218
        assert max(ms for _, ms in t.gaps) == 56232
        assert t.summary() == "177.9 s recorded, 50 gaps >= 10 ms, 89.2 s silent"


if __name__ == "__main__":
    unittest.main()
