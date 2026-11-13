"""Battery bench: battery-log parsing, the discharge table, run ends, and the
`arb bench` command line through the action group."""

from __future__ import annotations

import contextlib
import io
import itertools
import random
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

import auto_rockbox.commands.bench  # noqa: F401  (registers `arb bench`)
from auto_rockbox import bench, cli
from auto_rockbox.registry import Context
from auto_rockbox.workspace import Workspace

ROOT = Path(__file__).resolve().parents[3]          # the port's checkout
RA1 = ROOT / "build/battery/battlog-RA1.csv"
TS17_SA1 = ROOT / "build/battery/ts17-linein-SA1.csv"   # TS17 stock, wired
TS17_RA1 = ROOT / "build/battery/ts17-linein-RA1.csv"   # TS17 Rockbox, wired
RA1_TABLE = [3350, 3551, 3690, 3791, 3834, 3860, 3890, 3976, 4006, 4021, 4075]

HEADER = "uptime_s,mv,level,playing,charging,cpu_mhz\n"


def battlog(directory: Path) -> Path:
    """A short boot, then a run falling 1 mV a minute from 4200 to 3300 and
    powering off, then a boot after it"""
    lines = ["# boot x volume 0\n", HEADER, "60,4100,90,0,0,64\n", "120,4101,90,0,0,64\n"]
    lines += ["# boot x volume 0\n", HEADER]
    lines += [f"{60 * i},{4200 - i},50,1,0,64\n" for i in range(1, 901)]
    lines += ["# poweroff 54030,3299,0,1,0,192\n", "# boot x volume 0\n", HEADER,
              "60,3500,10,0,0,64\n"]
    p = directory / "battlog.csv"
    p.write_text("".join(lines))
    return p


def arb(ws: Workspace, *argv: str) -> tuple[int, str]:
    out = io.StringIO()
    args = cli.parser(ws).parse_args(list(argv))
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
        rc = args._command.fn(Context(ws), args)
    return rc or 0, out.getvalue()


class TestCurve(unittest.TestCase):
    def test_read_battlog(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            boots = bench.read_battlog(battlog(Path(d)))
            assert [len(b.uptime) for b in boots] == [2, 901, 1]
            run = bench.run_boot(battlog(Path(d)))
            assert run.poweroff == 54030 and run.end == 54030 and run.mv[-1] == 3299

    def test_table(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            run = bench.run_boot(battlog(Path(d)))
            t0 = bench.crossing(run, 4100)
            assert t0 == 6000
            table = bench.discharge_table(run, t0)
            assert table[-1] == 4100 and table[0] == 3300, table
            assert all(79 <= b - a <= 81 for a, b in itertools.pairwise(table[1:])), table

    @unittest.skipUnless(RA1.exists(), "the R-A 1 battery log is not in this checkout")
    def test_ra1_reference(self) -> None:
        """docs/battery-benchmark.md "Results": t0 at the 4075 mV crossing"""
        run = bench.run_boot(RA1)
        t0 = bench.crossing(run, 4075)
        assert t0 == 2825
        assert abs((run.end - t0) / 3600 - 19.29) < 0.005
        assert bench.discharge_table(run, t0) == RA1_TABLE

    def test_command(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d))
            log = battlog(Path(d))
            start = datetime(2026, 1, 1, tzinfo=UTC)
            end = start + timedelta(seconds=54030 - 6000)
            rc, out = arb(ws, "bench", "curve", str(log), "--start", start.isoformat(),
                          "--end", end.isoformat())
            assert rc == 0 and "t0 at 6000 s" in out and out.rstrip().endswith("4100"), out
            rc, _ = arb(ws, "bench", "curve", str(log), "--end", end.isoformat())
            assert rc == 1


class TestRunEnd(unittest.TestCase):
    def test_run_end(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=UTC)
        rows = [f"{(t0 + timedelta(seconds=i)).isoformat()},{i},-30.00,-20.00,-30.00,-20.00"
                for i in range(100)]
        rows += [f"{(t0 + timedelta(seconds=i)).isoformat()},{i},,,," for i in range(100, 110)]
        rows += [f"{(t0 + timedelta(seconds=i)).isoformat()},{i},-70.00,-60.00,-70.00,-60.00"
                 for i in range(110, 120)]
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "level.csv"
            log.write_text("\n".join(rows) + "\n")
            r = bench.run_end(log, t0 + timedelta(seconds=10), quiet=20)
            assert r is not None and r.quiet == 20 and r.logged == 110
            assert r.last_sound == t0 + timedelta(seconds=99)
            assert bench.run_end(log, t0, quiet=21) is None
            rc, out = arb(Workspace(Path(d)), "bench", "end", str(log), t0.isoformat(),
                          "--quiet", "20")
            assert rc == 0 and "0.03 h" in out, out          # 99 s


T0 = datetime(2026, 1, 1, tzinfo=UTC)
Row = tuple[float, float] | None        # (left RMS, peak over RMS) dB; None: digital silence


def music(seconds: int) -> list[Row]:
    """A made-up playlist: -40 dB give or take, peaks ~13 dB over the RMS,
    a second of digital silence between tracks every 200 s"""
    rng = random.Random(1)
    return [None if i % 200 == 199 else (-40 + rng.uniform(-8, 8), 13 + rng.uniform(-4, 4))
            for i in range(seconds)]


def tone(db: float) -> list[Row]:
    """The level tone: a partial first second, 10 s at DB, 10 s 12 dB lower"""
    return [(db - 4, 6.0)] + [(db, 3.1)] * 9 + [(db - 6, 5.0)] + [(db - 12, 3.1)] * 9


def silence(seconds: int) -> list[Row]:
    return [(-80.0, 10.0)] * seconds


def level_log(directory: Path, name: str, rows: list[Row]) -> Path:
    """A level log from T0; the right channel 0.2 dB under the left"""
    lines = []
    for i, r in enumerate(rows):
        cols = ",,," if r is None else \
            f"{r[0]:.2f},{r[0] + r[1]:.2f},{r[0] - 0.2:.2f},{r[0] - 0.2 + r[1]:.2f}"
        lines.append(f"{(T0 + timedelta(seconds=i)).isoformat()},{i},{cols}")
    p = directory / name
    p.write_text("\n".join(lines) + "\n")
    return p


class TestLevelTone(unittest.TestCase):
    def test_tones(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            log = level_log(Path(d), "run.csv", silence(40) + tone(-36) + music(600)
                            + tone(-46) + music(300))
            found = bench.tones(log, T0)
            assert [t.at for t in found] == [T0 + timedelta(seconds=41),
                                            T0 + timedelta(seconds=661)], found
            assert [round(v, 1) for t in found for v in t.db] == [-36, -36.2, -46, -46.2]
            assert bench.tones(log, T0 + timedelta(seconds=70)) == [found[1]]
            assert bench.first_sound(log, T0) == T0 + timedelta(seconds=40)

    def test_watch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            out: list[str] = []

            def watch(rows: list[Row]) -> int:
                out.clear()
                log = level_log(Path(d), "run.csv", rows)
                return bench.watch(log, T0, tone_db=-36.0, echo=out.append, poll=0)

            assert watch(silence(40) + tone(-36.3) + music(800) + silence(400)) == 0, out
            assert out[0].startswith("t0 ") and "L -36.3 R -36.5 dBFS, want -36.0" in out[1]
            assert "run ended" in out[2], out
            assert watch(silence(40) + tone(-46) + music(800)) == 3, out
            assert "L -46.0" in out[-1], out
            assert watch(silence(40) + music(800)) == 3 and "no level tone" in out[-1], out
            # a volume change after the start shows at the next play
            assert watch(tone(-36) + music(600) + tone(-38) + music(100)) == 3, out
            assert len(out) == 3 and "L -38.0" in out[-1], out
            rc, text = arb(Workspace(Path(d)), "bench", "watch", str(Path(d) / "run.csv"),
                           "--tone", "-36", "--t0", T0.isoformat())
            assert rc == 3 and "L -38.0" in text, text

    @unittest.skipUnless(TS17_SA1.exists() and TS17_RA1.exists(),
                         "the TS17 S-A 1 and R-A 1 level logs are not in this checkout")
    def test_ts17_logs(self) -> None:
        """docs/ts17-battery.md: stock's level setting played the tone at steps
        31, 22 and 20 (-24.8, -34.0, -36.0 dBFS); a day of music has none"""
        found = bench.tones(TS17_SA1, datetime(2026, 11, 11, 18, tzinfo=UTC))
        assert sorted({round(t.db[0], 1) for t in found}) == [-36.0, -34.0, -24.8], found
        assert not bench.tones(TS17_RA1, datetime(2026, 11, 13, 3, 44, 39, tzinfo=UTC))


if __name__ == "__main__":
    unittest.main()
