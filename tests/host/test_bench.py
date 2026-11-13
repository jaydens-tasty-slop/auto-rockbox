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
TS17_RA1 = ROOT / "build/battery/ts17-linein-RA1.csv"   # Rockbox at -21 dB, not -11
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


def music(seconds: int, seed: int = 1) -> list[float]:
    """Per-second RMS of a made-up playlist: -40 dB give or take, with a
    second of digital silence between tracks every 200 s"""
    rng = random.Random(seed)
    return [-100.0 if i % 200 == 199 else -40 + rng.uniform(-8, 8) for i in range(seconds)]


def level_log(directory: Path, name: str, levels: list[float], start: datetime = T0,
              silent_lead: int = 0) -> Path:
    rows = [f"{(start + timedelta(seconds=i)).isoformat()},{i},-80.00,-70.00,-80.00,-70.00"
            for i in range(silent_lead)]
    for i, db in enumerate(levels, silent_lead):
        v = "" if db <= -100 else f"{db:.2f}"
        rows.append(f"{(start + timedelta(seconds=i)).isoformat()},{i},{v},,{v},")
    p = directory / name
    p.write_text("\n".join(rows) + "\n")
    return p


class TestLevelMatch(unittest.TestCase):
    def test_level_match(self) -> None:
        tune = music(2000)
        with tempfile.TemporaryDirectory() as d:
            ref = level_log(Path(d), "ref.csv", tune)
            # the run starts 40 s later in the log and 25 s further into the playlist
            same = level_log(Path(d), "same.csv", [x + 0.3 for x in tune[25:]], silent_lead=40)
            low = level_log(Path(d), "low.csv", [x - 10 for x in tune], silent_lead=40)
            other = level_log(Path(d), "other.csv", music(2000, seed=2), silent_lead=40)
            quiet = level_log(Path(d), "quiet.csv", [x - 30 for x in tune])
            t0 = T0 + timedelta(seconds=40)
            m = bench.level_match(same, t0, ref, T0)
            assert m is not None and m.ok() and m.lag == 25 and m.corr > 0.99, m
            assert abs(m.diff[0] - 0.3) < 0.01 and abs(m.diff[1] - 0.3) < 0.01, m
            m = bench.level_match(low, t0, ref, T0)
            assert m is not None and not m.ok() and abs(m.diff[0] + 10) < 0.01, m
            assert "-10.0 dB" in str(m), m
            m = bench.level_match(other, t0, ref, T0)
            assert m is not None and not m.ok() and "not the reference's" in str(m), m
            m = bench.level_match(quiet, T0, ref, T0)
            assert m is not None and not m.ok() and "far quieter" in str(m), m
            assert bench.level_match(same, T0 + timedelta(seconds=1500), ref, T0) is None
            assert bench.first_sound(low, T0) == t0

    def test_reference_gap(self) -> None:
        """A reference silent for its first 90 s (a cable re-seated) still
        compares over the seconds where both play"""
        tune = music(2000)
        with tempfile.TemporaryDirectory() as d:
            ref = level_log(Path(d), "ref.csv", [-80.0] * 90 + tune[90:])
            run = level_log(Path(d), "run.csv", tune)
            m = bench.level_match(run, T0, ref, T0)
            assert m is not None and m.ok() and m.lag == 0 and m.both < 600, m

    def test_watch(self) -> None:
        tune = music(800)
        with tempfile.TemporaryDirectory() as d:
            ref = level_log(Path(d), "ref.csv", tune)
            out: list[str] = []
            run = level_log(Path(d), "run.csv", tune + [-80.0] * 400, silent_lead=40)
            rc = bench.watch(run, T0, ref=(ref, T0), echo=out.append, poll=0)
            assert rc == 0 and out[0].startswith("t0 ") and "+0.0 dB" in out[1], out
            assert "run ended" in out[2], out
            out.clear()
            low = level_log(Path(d), "low.csv", [x - 10 for x in tune] + [-80.0] * 400)
            assert bench.watch(low, T0, ref=(ref, T0), echo=out.append, poll=0) == 3, out
            rc, text = arb(Workspace(Path(d)), "bench", "match", str(low), T0.isoformat(),
                           str(ref), T0.isoformat())
            assert rc == 3 and "-10.0 dB" in text, text

    @unittest.skipUnless(TS17_SA1.exists() and TS17_RA1.exists(),
                         "the TS17 S-A 1 and R-A 1 level logs are not in this checkout")
    def test_ts17_ra1(self) -> None:
        """docs/ts17-battery.md: R-A 1 played ~10 dB under S-A 1 (its volume
        stayed at -21); S-A 1's Line In was silent until 19:19:26"""
        ra1 = datetime(2026, 11, 13, 3, 44, 39, tzinfo=UTC)
        sa1 = datetime(2026, 11, 11, 19, 17, tzinfo=UTC)
        m = bench.level_match(TS17_RA1, ra1, TS17_SA1, sa1)
        assert m is not None and not m.ok() and -10.5 < m.diff[0] < -9.5, m
        later = bench.level_match(TS17_RA1, ra1 + timedelta(seconds=3 * 8460), TS17_RA1, ra1)
        assert later is not None and later.ok(), later         # its 4th pass against its 1st


if __name__ == "__main__":
    unittest.main()
