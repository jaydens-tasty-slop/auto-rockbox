"""Line In: publishing (levels, WAV, JSON, ring slots) and reading back by
sequence number, with synthetic seconds."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np

from auto_rockbox import linein

T0 = datetime(2026, 11, 5, 12, 0, 0, tzinfo=UTC)


def second(left: float = 1000.0, right: float = 1500.0, *, start: float = 0.0,
           end: float = 1.0, gap_ms: tuple[int, int] | None = None, amp: float = 0.25) -> bytes:
    """One second of 16-bit stereo: a tone on each channel between START and END"""
    t = np.arange(linein.RATE) / linein.RATE
    a = amp * np.stack([np.sin(2 * np.pi * left * t), np.sin(2 * np.pi * right * t)], axis=1)
    a[(t < start) | (t >= end)] = 0
    if gap_ms:
        a[gap_ms[0] * 48:gap_ms[1] * 48] = 0
    return (a * 32767).astype("<i2").tobytes()


SILENCE = bytes(linein.RATE * 4)


def publish_all(directory: Path, seconds: list[tuple[bytes, datetime]],
                history: int = 300, first_seq: int = 1) -> None:
    pub = linein.Publisher(directory, history, "test.source", "line")
    for seq, (raw, when) in enumerate(seconds, first_seq):
        pub.publish(seq, raw, when, float(seq))


class TestPublisher(unittest.TestCase):
    def test_levels_and_row(self) -> None:
        lv = linein.levels(second(amp=1.0, right=1000.0)[:linein.RATE * 4])
        assert lv[0].peak == 32767
        assert abs((lv[0].rms_dbfs or 0) - (-3.01)) < 0.02
        quiet = linein.levels(SILENCE)
        assert quiet[0].rms_dbfs is None and quiet[0].peak_dbfs is None
        row = linein.level_row(T0, 7, [lv[0], quiet[1]])
        assert row == f"{T0.isoformat()},7,-3.01,-0.00,,\n", row

    def test_ring_and_latest(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            publish_all(root, [(second(), T0 + timedelta(seconds=i)) for i in range(6)],
                        history=4)
            slots = sorted(p.name for p in (root / "history").iterdir())
            assert slots == ["000", "001", "002", "003"], slots
            meta = json.loads((root / "history/000/audio.json").read_text())
            assert meta["sequence"] == 5 and meta["history_slot"] == 0
            assert meta["frames"] == linein.RATE and len(meta["levels"]) == 2
            assert set(meta["levels"][0]) == {"peak", "rms", "peak_dbfs", "rms_dbfs",
                                              "clipped_samples"}
            assert json.loads((root / "latest.json").read_text())["sequence"] == 6
            got = linein.history(root)
            assert [s.seq for s in got] == [3, 4, 5, 6]
            x, rate = got[-1].samples()
            assert rate == linein.RATE and x.shape == (linein.RATE, 2)


class TestReading(unittest.TestCase):
    def test_select(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            # an earlier publisher run (a ring of 10) left #5 and #6 in slots 4 and 5
            publish_all(root, [(SILENCE, T0 - timedelta(days=1, seconds=-i)) for i in range(2)],
                        history=10, first_seq=5)
            # this run (a ring of 4) reached #6 too; #5 and #6 share a wall second
            stamps = [T0 + timedelta(seconds=i) for i in range(4)]
            stamps += [T0 + timedelta(seconds=4.1), T0 + timedelta(seconds=4.8)]
            publish_all(root, [(SILENCE, t) for t in stamps], history=4)
            seconds = linein.history(root)
            assert [s.seq for s in seconds] == [5, 6, 3, 4, 5, 6]
            last = linein.select(seconds, ["2"])
            assert [str(s) for s in last] == ["#5 12:00:04.100", "#6 12:00:04.800"]
            window = linein.select(seconds, [stamps[2].isoformat(), stamps[3].isoformat()])
            assert [s.seq for s in window] == [3, 4]
            assert linein.select(seconds, seq=[5, 6]) == last       # the newest #5 and #6
            assert linein.gaps_in(seconds) == [(6, 3)]

    def test_parse_when(self) -> None:
        late = datetime(2026, 11, 5, 0, 10, tzinfo=UTC)
        assert linein.parse_when("23:59:00", late) == datetime(2026, 11, 4, 23, 59, tzinfo=UTC)
        early = datetime(2026, 11, 4, 23, 50, tzinfo=UTC)
        assert linein.parse_when("00:05:00.5", early) == datetime(
            2026, 11, 5, 0, 5, 0, 500000, tzinfo=UTC)
        assert linein.parse_when("2026-11-01T01:02:03", late).tzinfo == UTC


class TestAnalysis(unittest.TestCase):
    def test_analyse_and_low_runs(self) -> None:
        x = np.frombuffer(second(gap_ms=(300, 310)), "<i2").reshape(-1, 2) / 32768
        ch = linein.analyse(x[:, 1], linein.RATE)
        assert abs(ch.hz - 1500) < 0.5 and ch.purity > 0.9 and abs(ch.db + 15.05) < 0.1
        runs = linein.low_runs(x[:, 0], linein.RATE)
        assert runs == [(300, 10)], runs
        assert linein.tone_kind(1000.0, 1500.0) == "stereo"
        assert linein.tone_kind(1500.0, 1000.0) == "tone"
        assert linein.tone_kind(1000.0, 440.0) == "music"

    def test_tone_check(self) -> None:
        """Tone #4..#16; #4 and #16 partial (with a gap in #16 that must not
        count); a dropout in #9, published in the same wall second as #10"""
        plan: list[bytes] = [SILENCE] * 3 + [second(start=0.5)]
        plan += [second(gap_ms=(400, 410)) if seq == 9 else second() for seq in range(5, 16)]
        plan += [second(end=0.6, gap_ms=(200, 220)), SILENCE, SILENCE]
        stamps = [T0 + timedelta(seconds=seq) for seq in range(1, len(plan) + 1)]
        stamps[8] = T0 + timedelta(seconds=9.05)          # #9 and #10 share 12:00:09
        stamps[9] = T0 + timedelta(seconds=9.95)
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            publish_all(root, list(zip(plan, stamps, strict=True)))
            r = linein.tone_check(root, T0 - timedelta(seconds=3), T0 + timedelta(seconds=17))
            assert [h.second.seq for h in r.tone] == list(range(4, 17))
            assert [h.second.seq for h in r.dropouts] == [9], r.report()
            assert r.problems == [f"dropout {r.dropouts[0].low_line()}"]
            assert r.note == "13 tone s"
            early = linein.judge(r.heard, span=4)
            assert early.dropouts == [] and early.problems == []
            assert linein.judge(r.heard, need=20).problems[0] == "tone seen in 13 s only"

    def test_mono_tone(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            publish_all(root, [(second(1000.0, 1000.0), T0 + timedelta(seconds=i))
                               for i in range(1, 9)])
            heard = [linein.hear(s) for s in linein.history(root)]
            assert linein.judge(heard, stereo=True).tone == []
            assert len(linein.judge(heard, stereo=False).tone) == 8


if __name__ == "__main__":
    unittest.main()
