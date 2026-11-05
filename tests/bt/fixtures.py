"""The port's earlier range runs (build/xm3, written by one-off scripts before
auto_rockbox.bt), and converters from their formats to a run directory's.
They are read only; a test that needs one skips if it is absent."""

from __future__ import annotations

import unittest
from datetime import date, datetime, time
from pathlib import Path

from auto_rockbox.bt import bluez, rundir

ROOT = Path(__file__).resolve().parents[3]          # the port's checkout
XM3 = ROOT / "build/xm3"


def need(path: Path) -> Path:
    if not path.exists():
        raise unittest.SkipTest(f"{path} is not in this checkout")
    return path


def serial_to_walk(src: Path, dst: Path, day: date) -> None:
    """serlog.py's "HH:MM:SS line" log as walk.txt"""
    with rundir.Log(dst, echo=None) as log:
        for line in src.read_text(errors="replace").splitlines():
            head, _, rest = line.partition(" ")
            try:
                t = datetime.combine(day, time.fromisoformat(head)).astimezone()
            except ValueError:
                continue
            log(rest, t)


def pcpoll_to_rssi(src: Path, dst: Path) -> None:
    """pcpoll.sh's "DATE_TIME btmgmt-output" log as pc-rssi.txt"""
    with rundir.Log(dst, echo=None) as log:
        for line in src.read_text(errors="replace").splitlines():
            head, _, rest = line.partition(" ")
            ci = bluez.parse_conn_info(rest)
            log(ci.line() if ci else "none",
                datetime.strptime(head, "%Y-%m-%d_%H:%M:%S").astimezone())
