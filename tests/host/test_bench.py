"""Battery bench: battery-log parsing, the discharge table, run ends, and the
`arb bench` command line through the action group."""

from __future__ import annotations

import contextlib
import io
import itertools
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


if __name__ == "__main__":
    unittest.main()
