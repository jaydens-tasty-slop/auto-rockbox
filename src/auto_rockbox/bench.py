"""Battery-run helpers: a per-second level log of an audio source, the end of
a playback run read from that log, the end of a Bluetooth run read from the
link, and the discharge table from the player's battery log.

A run plays music until the player powers itself off. Its end on this PC is
the music going silent (Line In, or the A2DP stream routed to a null sink),
which the level log records; a Bluetooth run also ends when the link drops
and the player no longer answers a page.

    [bench]
    sink = "arb_null"   # the null sink a Bluetooth run's stream plays into;
                        # pactl load-module module-null-sink sink_name=arb_null
"""

from __future__ import annotations

import csv
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from . import linein
from .bt import bluez
from .errors import ArbError

A2DP_RATE = 44100               # the stream's own rate: no resampling in the log
POLL_S = 10
PAGE_S = 300                    # how long a dropped player is paged
PAGE_RETRY_S = 20
CURVE_WINDOW_S = 120            # a table entry is the median over +-this


class BenchError(ArbError):
    pass


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------- level log

def level_log(source: str, log: Path, rate: int = A2DP_RATE) -> None:
    """Appends a level line a second (linein.level_row) until parec stops"""
    with log.open("a", buffering=1) as f:
        for seq, raw in enumerate(linein.parec(source, rate, latency_ms=500), 1):
            f.write(linein.level_row(_now(), seq, linein.levels(raw)))


@dataclass(frozen=True)
class RunEnd:
    start: datetime
    last_sound: datetime
    logged: int                 # seconds logged since the start
    quiet: int                  # silent seconds at the end

    @property
    def hours(self) -> float:
        return (self.last_sound - self.start).total_seconds() / 3600

    def __str__(self) -> str:
        return (f"run ended: start {self.start.isoformat()}, last sound "
                f"{self.last_sound.isoformat()}, {self.hours:.2f} h ({self.logged} logged "
                f"seconds, {self.quiet} s silent at the end)")


def run_end(log: Path, start: datetime, quiet: int = 300) -> RunEnd | None:
    """The run's end once the level log has QUIET silent seconds (left RMS
    empty or below linein.SILENT_DB) after START; None while it still plays.
    Seconds are counted as logged: a gap in the log (the PC asleep) is not
    silence."""
    last_loud, silent, logged = None, 0, 0
    with log.open() as f:
        for row in csv.reader(f):
            t = datetime.fromisoformat(row[0])
            if t < start:
                continue
            logged += 1
            if row[2] and float(row[2]) > linein.SILENT_DB:
                last_loud, silent = t, 0
            else:
                silent += 1
    if last_loud and silent >= quiet:
        return RunEnd(start, last_loud, logged, silent)
    return None


# ---------------------------------------------------------------- Bluetooth

def bt_watch(addr: str, log: Path, sink: str, echo: Callable[[str], object] = print) -> int:
    """Polls the link every POLL_S s, appending "utc,connected" to LOG, and
    keeps the player's stream on SINK. After a drop the PC pages the player
    for PAGE_S s (stock firmware does not reconnect by itself): 0 if it never
    answers (the run is over), 2 if it does (a range drop: check the run)."""
    if bluez.sink_index(sink) is None:
        raise BenchError(f"no sink {sink}: pactl load-module module-null-sink sink_name={sink}")
    last_up: datetime | None = None
    for ev in bluez.watch_link(addr, period=POLL_S, page_for=PAGE_S, page_every=PAGE_RETRY_S,
                               sink=sink):
        at = ev.at.astimezone(UTC)
        if ev.kind == "poll":
            with log.open("a") as f:
                f.write(f"{at.isoformat(timespec='seconds')},{int(ev.connected)}\n")
            if ev.connected:
                last_up = at
        elif ev.kind == "back":
            since = last_up.isoformat(timespec="seconds") if last_up else "?"
            echo(f"dropped after {since}, reconnected {at.isoformat(timespec='seconds')}: "
                 "check the stream")
            return 2
        elif ev.kind == "over":
            since = last_up.isoformat(timespec="seconds") if last_up else "?"
            echo(f"run over: last connected {since}")
            return 0
    return 0


# ---------------------------------------------------------------- discharge curve

@dataclass(frozen=True)
class Boot:
    """One boot's samples from a Rockbox battery log"""

    uptime: np.ndarray          # s
    mv: np.ndarray
    poweroff: float | None      # uptime of its power-off line, if it ended in one

    @property
    def end(self) -> float:
        return self.poweroff if self.poweroff is not None else float(self.uptime[-1])


def read_battlog(path: Path) -> list[Boot]:
    """The boots of a battery log, in order. Sample lines start with a digit
    (uptime_s,mv,level,playing,charging,cpu_mhz); "# poweroff UPTIME,mv,..."
    is a last sample that ends its boot; other lines are headers. A boot
    starts at a "# boot" line or where the uptime falls."""
    boots: list[Boot] = []
    rows: list[tuple[float, float]] = []
    poweroff: float | None = None

    def close() -> None:
        nonlocal rows, poweroff
        if rows:
            a = np.array(rows)
            boots.append(Boot(a[:, 0], a[:, 1], poweroff))
        rows, poweroff = [], None

    for line in path.read_text().splitlines():
        is_off = line.startswith("# poweroff ")
        if not (line[:1].isdigit() or is_off):
            if line.startswith("# boot"):
                close()
            continue
        up, mv = (float(v) for v in line.removeprefix("# poweroff ").split(",")[:2])
        if poweroff is not None or (rows and up < rows[-1][0]):
            close()
        rows.append((up, mv))
        if is_off:
            poweroff = up
    close()
    return boots


def run_boot(path: Path) -> Boot:
    """The boot that is the run: the longest"""
    boots = read_battlog(path)
    if not boots:
        raise BenchError(f"no samples in {path}")
    return max(boots, key=lambda b: b.end - float(b.uptime[0]))


def crossing(boot: Boot, mv: float) -> float:
    """The uptime of the first sample at or below MV"""
    below = np.flatnonzero(boot.mv <= mv)
    if not len(below):
        raise BenchError(f"the log never reads {mv:.0f} mV or less")
    return float(boot.uptime[below[0]])


def discharge_table(boot: Boot, t0: float, window: float = CURVE_WINDOW_S) -> list[int]:
    """percent_to_volt_discharge, 0 to 100 % in 10 % steps. Percent remaining
    at uptime t is 100 * (end - t) / (end - t0); each entry is the median mV
    within WINDOW s of its time (one ADC reading is 14 mV coarse and noisy)."""
    out = []
    for p in range(0, 101, 10):
        t = boot.end - p / 100 * (boot.end - t0)
        near = boot.mv[np.abs(boot.uptime - t) <= window]
        if not len(near):
            raise BenchError(f"no samples within {window:.0f} s of {p} % (uptime {t:.0f} s)")
        out.append(round(float(np.median(near))))
    return out
