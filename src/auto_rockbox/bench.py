"""Battery-run helpers: a per-second level log of an audio source, the start
and end of a playback run read from that log, its music level against a
reference run's, the end of a Bluetooth run read from the link, and the
discharge table from the player's battery log.

A run plays music until the player powers itself off. Its end on this PC is
the music going silent (Line In, or the A2DP stream routed to a null sink),
which the level log records; a Bluetooth run also ends when the link drops
and the player no longer answers a page.

Runs compare only at the same output level. A tone check before the start
does not prove the run's level: a setting can fail to take, so the run's
first minutes of music are compared with the reference run's (stock's) over
the same stretch of the playlist, and a run off by more than a decibel is
stopped there, not found out a day later.

    [bench]
    sink = "arb_null"   # the null sink a Bluetooth run's stream plays into;
                        # pactl load-module module-null-sink sink_name=arb_null
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
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
MATCH_S = 600                   # the run's first seconds of music compared with the reference
MATCH_LAG_S = 300               # how far apart the two starts may be in the playlist
MATCH_DB = 1.0                  # a level difference that stops the run
MATCH_CORR = 0.7                # below this the run is not playing the reference's music
FLOOR_DB = -100.0               # digital silence (an empty field) in the comparison
WATCH_POLL_S = 30


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


def first_sound(log: Path, after: datetime) -> datetime | None:
    """The first second at or after AFTER whose left RMS is above
    linein.SILENT_DB: a run's t0 once play is pressed"""
    with log.open() as f:
        for row in csv.reader(f):
            t = datetime.fromisoformat(row[0])
            if t >= after and row[2] and float(row[2]) > linein.SILENT_DB:
                return t
    return None


def _rms_rows(log: Path, start: datetime) -> tuple[list[datetime], np.ndarray]:
    """The level log from START on: each logged second's time and (L, R)
    RMS dBFS, digital silence at FLOOR_DB"""
    times: list[datetime] = []
    rows: list[list[float]] = []
    with log.open() as f:
        for row in csv.reader(f):
            t = datetime.fromisoformat(row[0])
            if t >= start:
                times.append(t)
                rows.append([float(v) if v else FLOOR_DB for v in (row[2], row[4])])
    return times, np.array(rows).reshape(-1, 2)


def _mean_db(a: np.ndarray) -> tuple[float, float]:
    """Mean power of per-second RMS dB values, L and R"""
    p = 10 * np.log10(np.mean(10 ** (a / 10), axis=0))
    return float(p[0]), float(p[1])


@dataclass(frozen=True)
class LevelMatch:
    """A run's first seconds of music against the reference run's over the
    same stretch of the playlist, both measured over the seconds where both
    play (a reference with a gap, say a re-seated cable, still compares)"""

    run_db: tuple[float, float]     # mean power, L and R, dBFS
    ref_db: tuple[float, float]
    lag: int                        # s: the stretch's start from the reference's t0
    corr: float                     # per-second left RMS, run against reference
    both: int                       # seconds that play in both
    seconds: int

    @property
    def diff(self) -> tuple[float, float]:
        return self.run_db[0] - self.ref_db[0], self.run_db[1] - self.ref_db[1]

    def ok(self, tolerance: float = MATCH_DB) -> bool:
        return self.corr >= MATCH_CORR and all(abs(d) <= tolerance for d in self.diff)

    def __str__(self) -> str:
        if self.both < self.seconds // 2:
            return (f"only {self.both} of the first {self.seconds} s play in both runs: "
                    "the run is silent or far quieter than the reference")
        if self.corr < MATCH_CORR:
            return (f"the music is not the reference's (best match {self.corr:.2f} over "
                    f"{self.seconds} s): other files, order or shuffle")
        (rl, rr), (fl, fr), (dl, dr) = self.run_db, self.ref_db, self.diff
        return (f"level over the first {self.seconds} s: L {rl:.1f} R {rr:.1f} dBFS, "
                f"reference L {fl:.1f} R {fr:.1f}: L {dl:+.1f} R {dr:+.1f} dB "
                f"(reference stretch at {self.lag:+d} s, match {self.corr:.2f}, "
                f"{self.both} s in both)")


def level_match(log: Path, t0: datetime, ref: Path, ref_t0: datetime, *,
                seconds: int = MATCH_S, max_lag: int = MATCH_LAG_S) -> LevelMatch | None:
    """The run's first SECONDS logged seconds from T0 against the reference
    run's; None until the run has that many. The reference's stretch is the
    one starting within MAX_LAG s of REF_T0 whose per-second left RMS
    correlates best with the run's over the seconds where both play, so the
    two starts need not line up."""
    run = _rms_rows(log, t0)[1][:seconds]
    if len(run) < seconds:
        return None
    times, rows = _rms_rows(ref, ref_t0 - timedelta(seconds=max_lag))
    s0 = next((i for i, t in enumerate(times) if t >= ref_t0), len(times))
    if len(rows) - s0 < seconds:
        raise BenchError(f"{ref} has fewer than {seconds} s from {ref_t0.isoformat()}")
    best = LevelMatch(_mean_db(run), _mean_db(rows[s0:s0 + seconds]), 0, 0.0, 0, seconds)
    with np.errstate(invalid="ignore", divide="ignore"):
        for d in range(-min(max_lag, s0), max_lag + 1):
            seg = rows[s0 + d:s0 + d + seconds]
            if len(seg) < seconds:
                break
            both = (run[:, 0] > linein.SILENT_DB) & (seg[:, 0] > linein.SILENT_DB)
            n, enough = int(both.sum()), best.both >= seconds // 2
            if n < seconds // 2:
                if not enough and n > best.both:
                    best = replace(best, lag=d, both=n)
                continue
            c = float(np.nan_to_num(np.corrcoef(run[both, 0], seg[both, 0])[0, 1]))
            if not enough or c > best.corr:
                best = LevelMatch(_mean_db(run[both]), _mean_db(seg[both]), d, c, n, seconds)
    return best


def watch(log: Path, after: datetime, *, ref: tuple[Path, datetime] | None = None,
          t0: datetime | None = None, tolerance: float = MATCH_DB, quiet: int = 300,
          echo: Callable[[str], object] = print, poll: float = WATCH_POLL_S) -> int:
    """Follows a run's level log to its end: t0 (T0, or the first loud second
    after AFTER), then its first MATCH_S s against REF (the reference run's
    log and t0), then the end (QUIET silent seconds). 0: the run is over;
    3: its level or music is not the reference's, so stop it now."""
    start, checked = t0, ref is None
    while True:
        if start is None and (start := first_sound(log, after)) is not None:
            echo(f"t0 {start.isoformat()}: music started")
        if start is not None and ref is not None and not checked:
            m = level_match(log, start, *ref)
            if m is not None:
                echo(str(m))
                if not m.ok(tolerance):
                    return 3
                checked = True
        if start is not None and (r := run_end(log, start, quiet)) is not None:
            echo(str(r))
            return 0
        time.sleep(poll)


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
