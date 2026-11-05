"""Gaps in a player's A2DP stream as this PC receives it (the PC as the
sink): runs of exact-zero samples on both channels.

BlueZ and PipeWire fill what the link did not deliver with digital silence,
and music has exact-zero runs only at track boundaries (where the level
around them is low), so a zero run of a few ms or more is a dropout. The live
logger records the player's BlueZ input node with pw-record and keeps no
audio; the offline scan reads a recording. Both write the same log
(rundir.GAPS):

    STAMP node NAME              recording started on NAME
    STAMP level N DBFS ZERO_MS   second N: RMS level (both channels), ms of zeros
    STAMP gap MS                 a zero run of MS ms that started at STAMP
    STAMP lost NAME              the node went away (the link or stream is gone)

Live stamps come from the wall clock as each second arrives, so they are as
good as PipeWire's delivery (a quantum, ~20 ms); a recording's come from its
start time and the sample count.
"""

from __future__ import annotations

import os
import re
import selectors
import subprocess
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from ..errors import ArbError
from . import bluez, rundir

RATE = 44100            # SBC's rate on these players; zeros stay zeros if PipeWire resamples
CHANNELS = 2
MIN_MS = 10
NO_DATA_S = 3.0         # this long without samples: check that the node is still there

Write = Callable[[str, datetime], None]
_HEADER = re.compile(r"gaps: zero runs >= (\d+) ms")


class GapError(ArbError):
    pass


def header(min_ms: int) -> str:
    """The log's first comment: what counts as a gap"""
    return f"gaps: zero runs >= {min_ms} ms on both channels"


def logged_min_ms(text: str) -> int | None:
    m = _HEADER.search(text)
    return int(m.group(1)) if m else None


@dataclass
class Tracker:
    """Zero runs and per-second levels of a stream fed one second at a time;
    a run may span seconds"""

    write: Write
    rate: int = RATE
    min_ms: int = MIN_MS
    gaps: list[tuple[datetime, int]] = field(default_factory=list)
    seconds: int = 0
    frames: int = 0
    _run: int = 0                       # frames in the zero run still open
    _run_start: datetime | None = None

    def feed(self, block: np.ndarray, start: datetime) -> None:
        """BLOCK: int16 frames x channels, its first sample at START"""
        n = len(block)
        if not n:
            return
        z = (block == 0).all(axis=1)
        edges = np.diff(np.concatenate(([self._run > 0], z, [False])).astype(np.int8))
        starts: list[int | None] = [int(i) for i in np.flatnonzero(edges == 1)]
        ends = [int(i) for i in np.flatnonzero(edges == -1)]
        if self._run:
            starts.insert(0, None)              # the run open from the last block
        carried, carried_start = 0, None
        for s, e in zip(starts, ends, strict=True):
            if s is None:
                length, t0 = self._run + e, self._run_start
            else:
                length, t0 = e - s, start + timedelta(seconds=s / self.rate)
            assert t0 is not None
            if e == n:
                carried, carried_start = length, t0
            else:
                self._end_run(length, t0)
        self._run, self._run_start = carried, carried_start
        x = block.astype(np.float64)
        rms = 10 * np.log10(float(np.mean(x * x)) / 32768**2 + 1e-12)
        zero_ms = int(z.sum()) * 1000 // self.rate
        self.write(f"level {self.seconds} {rms:.1f} {zero_ms}", start)
        self.seconds += 1
        self.frames += n

    def close(self) -> None:
        """Ends a zero run still open: the stream stopped inside it"""
        if self._run and self._run_start is not None:
            self._end_run(self._run, self._run_start)
        self._run, self._run_start = 0, None

    def _end_run(self, frames: int, t0: datetime) -> None:
        if frames * 1000 >= self.min_ms * self.rate:
            ms = frames * 1000 // self.rate
            self.gaps.append((t0, ms))
            self.write(f"gap {ms}", t0)

    def summary(self) -> str:
        silent = sum(ms for _, ms in self.gaps)
        return (f"{self.frames / self.rate:.1f} s recorded, {len(self.gaps)} gaps >= "
                f"{self.min_ms} ms, {silent / 1000:.1f} s silent")


def scan_wav(path: Path, start: datetime, write: Write, min_ms: int = MIN_MS) -> Tracker:
    """The gaps in a 16-bit recording whose first sample was at START"""
    try:
        with path.open("rb") as f, wave.open(f) as w:
            if w.getsampwidth() != 2:
                raise GapError(f"{path}: {8 * w.getsampwidth()}-bit samples, 16 needed")
            rate, channels = w.getframerate(), w.getnchannels()
            t = Tracker(write, rate, min_ms)
            done = 0
            while raw := w.readframes(rate):
                block = np.frombuffer(raw, "<i2").reshape(-1, channels)
                t.feed(block, start + timedelta(seconds=done / rate))
                done += len(block)
    except (wave.Error, EOFError) as e:
        raise GapError(f"{path}: not a usable WAV file ({e})") from None
    t.close()
    return t


def record(node: str, addr: str | None, tracker: Tracker) -> None:
    """Feeds TRACKER one-second blocks of NODE until the node goes away.
    node.dont-reconnect keeps pw-record off other sources (a microphone)
    once the player's node is gone."""
    second = tracker.rate * CHANNELS * 2
    argv = ["pw-record", "--target", node, "--rate", str(tracker.rate),
            "--channels", str(CHANNELS), "--format", "s16",
            "-P", "node.dont-reconnect=true", "-"]
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as rec:
        assert rec.stdout is not None
        fd = rec.stdout.fileno()
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        buf = bytearray()
        heard = time.monotonic()
        try:
            while True:
                if not sel.select(1.0):
                    if (time.monotonic() - heard > NO_DATA_S
                            and bluez.input_node(addr) != node):
                        return
                    continue
                data = os.read(fd, second - len(buf))
                if not data:
                    return
                heard = time.monotonic()
                buf += data
                if len(buf) == second:
                    block = np.frombuffer(bytes(buf), "<i2").reshape(-1, CHANNELS)
                    tracker.feed(block, rundir.now() - timedelta(seconds=1))
                    buf.clear()
        finally:
            sel.close()
            rec.terminate()


def gaplog(out: Path, addr: str | None, min_ms: int = MIN_MS,
           echo: Callable[[str], object] | None = print) -> None:
    """Logs the gaps of the player's stream (ADDR's, or any BlueZ input) into
    OUT/gaps.txt until Ctrl-C, waiting for its node whenever it is gone"""
    with rundir.Log(out / rundir.GAPS, echo) as log:
        log.comment(header(min_ms))

        def write(text: str, at: datetime) -> None:
            log(text, at, quiet=text.startswith("level"))

        waiting = False
        while True:
            node = bluez.input_node(addr)
            if node is None:
                if not waiting and echo:
                    echo("waiting for the player's stream")
                waiting = True
                time.sleep(1)
                continue
            waiting = False
            log(f"node {node}")
            tracker = Tracker(write, RATE, min_ms)
            record(node, addr, tracker)
            tracker.close()
            log(f"lost {node}")
