"""The run report: one drop seen by several files, its recovery times, the
silence and rows; then two of the port's earlier runs against the numbers
docs/bluetooth.md gives for them."""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from auto_rockbox.bt import gaps, report, rundir

from .fixtures import XM3, need, pcpoll_to_rssi, serial_to_walk

T0 = datetime.fromisoformat("2026-10-29T22:00:00.000-06:00")


def at(s: float) -> datetime:
    return T0 + timedelta(seconds=s)


def write(path: Path, lines: list[tuple[float, str]], comment: str = "") -> None:
    with rundir.Log(path, echo=None) as log:
        if comment:
            log.comment(comment)
        for s, text in lines:
            log(text, at(s))


def synthetic(d: Path) -> Path:
    """A player walk where the link drops at 10 s: the player logs it, BlueZ
    sees it at 11 s, the stream's node goes at 12 s and is back at 20 s"""
    write(d / rundir.WALK, [
        (0, "walk: bt 3 audio 1"),
        (1, "bt: media 400 pkts bp 53 behind 0 ms waits 0 restarts 0 bufs 9/9"),
        (5, "bt link 0: master rssi -40 dBm txpwr 19 types ee1c"),
        (6, "bt: media 300 pkts bp 24 behind 200 ms waits 900 restarts 50 bufs 0/9"),
        (10, "bt: disconnected: 08"),
        (15, "bt: link lost: paging again (1)"),
        (20, "bt: connected, handle 128"),
        (22, "bt: streaming, 5 frames of 83 bytes per packet, bitpool 35"),
        (40, "walk: resumed"),
    ])
    write(d / rundir.CONN, [(0, "up"), (11, "down"), (21, "back")])
    write(d / rundir.RSSI, [(0, "rssi -5 tx 0 max 4"), (2, "rssi -15 tx 0 max 4"),
                            (12, "none"), (22, "rssi -10 tx 0 max 4")])
    lines: list[tuple[float, str]] = [(s, f"level {s} -27.5 0") for s in range(12)]
    lines += [(9.5, "gap 500"), (12, "lost bluez_input.x.2"), (20, "node bluez_input.x.2"),
              (20, "gap 1500")]
    lines += [(s, f"level {s - 20} -27.5 0") for s in range(20, 30)]
    write(d / rundir.GAPS, lines, gaps.header(10))
    return d


class ReportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.result = report.Run.read(synthetic(Path(self.dir.name)))

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_one_drop(self) -> None:
        (d,) = self.result.drops()
        assert d.at == at(10) and d.reason == 0x08
        assert d.sources == {"player", "conn", "gaps"}
        assert (d.paged, d.link, d.audio) == (at(15), at(20), at(21.5))
        assert d.line().endswith("[conn, gaps, player]: re-paged +5.0 s, link +10.0 s, "
                                 "audio +11.5 s"), d.line()

    def test_summary(self) -> None:
        text = "\n".join(self.result.summary())
        assert "silent: 10.0 s of 30.0 s (2 gaps >= 10 ms; the stream lost: 1)" in text
        assert "gaps: < 0.1 s 0, 0.1-0.3 s 0, 0.3-1 s 1, 1-5 s 1, >= 5 s 0" in text
        assert "first stutter (a gap >= 100 ms): 22:00:09.500, 9.5 s into" in text
        assert "drops: 1 (0x08 (connection timeout) x1)" in text
        assert "pc rssi: min -15, median -10, mean -10.0 over 3 readings (1 without" in text
        assert "player: bitpool 24-53 (median 38.5), ACL buffer waits 900, clock " \
               "restarts 50" in text

    def test_timeline(self) -> None:
        lines = [e.line() for e in self.result.timeline()]
        rows = [x for x in lines if "10 s " in x[:20]]
        assert rows[0] == ("22:00:00.000  10 s   silent  0.5 s | pc rssi -10 | bp 24 waits 900 "
                           "restarts 50 | player rssi -40"), rows[0]
        assert rows[1].startswith("22:00:10.000  10 s   silent  8.0 s | pc rssi none")
        assert rows[-1].startswith("22:00:20.000  10 s   silent  1.5 s")
        events = [x[14:] for x in lines if x not in rows]
        assert events[:3] == ["walk   bt 3 audio 1", "conn   up", "gaps   gap 500 ms"], events
        assert "player bt: disconnected: 08" in events
        assert "gaps   stream lost" in events

    def test_window(self) -> None:
        run = report.Run.read(Path(self.dir.name), at(15), at(30))
        assert run.drops() == []
        assert "silent: 6.5 s of 15.0 s" in "\n".join(run.summary())


class FixtureTest(unittest.TestCase):
    def test_garage_walk(self) -> None:
        """xm3fix, 2026-10-29: "the link lost (0x08, after an 8 s playback
        stall) at the far end of the garage ... Paged again 5 s later and
        streaming 6 s after that" """
        src = need(XM3 / "xm3fix/serial.txt")
        with tempfile.TemporaryDirectory() as d:
            serial_to_walk(src, Path(d) / rundir.WALK, date(2026, 10, 29))
            run = report.Run.read(Path(d))
            drops = run.drops()
            last = drops[-1]
            assert last.reason == 0x08 and rundir.clock(last.at) == "22:35:00.000"
            assert [(t - last.at).total_seconds() if t else None
                    for t in (last.paged, last.link, last.audio)] == [5.0, 10.0, 11.0]
            stall = [x for t, k, x in run.player.events if k == "stall" and t and
                     last.at - timedelta(seconds=10) < t <= last.at]
            assert stall[-1].startswith("STALL ended after 8090 ms")
            assert [d.reason for d in drops].count(0x13) == 9

    def test_stairs_rssi(self) -> None:
        """rssi-stairs, 2026-10-28, 2 minutes from placing it: ours -14.9 dB
        mean, 49.9 s silent, 106 gaps (of >= 50 ms)"""
        run_dir = need(XM3 / "rssi-stairs")
        start = datetime.fromisoformat((run_dir / "ours/started.txt").read_text().strip())
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            with rundir.Log(out / rundir.GAPS, echo=None) as log:
                log.comment(gaps.header(50))
                gaps.scan_wav(run_dir / "ours/stream.wav", start, log, 50)
            pcpoll_to_rssi(run_dir / "pc.txt", out / rundir.RSSI)
            placed = start.replace(hour=21, minute=57, second=26, microsecond=0)
            run = report.Run.read(out, placed, placed + timedelta(minutes=2))
            text = "\n".join(run.summary())
        assert "silent: 49.8 s of 120.0 s (105 gaps >= 50 ms)" in text, text
        assert "mean -14.8 over 60 readings" in text


if __name__ == "__main__":
    unittest.main()
