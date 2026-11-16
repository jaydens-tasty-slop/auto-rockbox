"""Line In: this PC's recording of a player's analog output.

`arb linein serve` records a PulseAudio/PipeWire source with parec and
publishes each second as it completes: DIR/latest.wav and latest.json, and
the same in a ring of slots, DIR/history/NNN/audio.wav and audio.json. A
second's JSON holds its sequence number, the host time it was published and
its levels.

Publication is bursty: two seconds can be published within one wall-clock
second, so a second is addressed by its sequence number (and a stamp to the
ms), never by HH:MM:SS alone. Stamps trail the player by about LAG_S.
Sequence numbers restart with the publisher; the ring can still hold slots
of an earlier run, with older stamps.

    [linein]
    dir = "analysis/audio"      # the publisher's output
    history = 300               # seconds the ring keeps
    source = "alsa_input.pci-0000_0c_00.4.analog-stereo"  # default: the one
    port = "analog-input-linein"                          # with a line port

The source must stay at its base volume (unity gain): a Line In source at
100% adds up to ~60 dB and the player's output clips. The publisher sets it.
"""

from __future__ import annotations

import io
import itertools
import json
import math
import os
import selectors
import subprocess
import time
import wave
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any, Literal

import numpy as np

from .errors import ArbError
from .publish import Ring, atomic_write, exclusive
from .workspace import Workspace

RATE = 48000
CHANNELS = 2
LAG_S = 3.0                 # a second's stamp trails the player by about this
TONES = (1000.0, 1500.0)    # the test tone: 1 kHz left, 1.5 kHz right
TONE_TOLERANCE_HZ = 8.0
SILENT_DB = -60.0           # a second this quiet (RMS, left) carries nothing
CLEAN = 0.5                 # share of a channel's power in its tone for a clean tone
DROPOUT_MS = 3              # more 1 ms windows than this low in a tone second: a dropout
FULL_DB = 6.0               # a tone second this close to the loudest is at full level
                            # (tone.wav's step-down at 10 s is 12 dB)
MIN_HZ = 50.0               # below this a spectrum peak is hum or offset, not the signal
LAST_DEFAULT = 60           # seconds a bare `arb linein analyse` looks at
LIVE_S = 5                  # the latest second is this fresh while the capture runs

Kind = Literal["stereo", "tone", "music"]


class CaptureError(ArbError):
    pass


def directory(ws: Workspace) -> Path:
    return ws.section("linein").path("dir", "analysis/audio")


def running(directory: Path) -> bool:
    """Whether `arb linein serve` is publishing seconds into DIRECTORY"""
    latest = directory / "latest.json"
    return latest.exists() and time.time() - latest.stat().st_mtime <= LIVE_S


# ---------------------------------------------------------------- capture

def sources() -> list[dict[str, Any]]:
    """The recording inputs, without the sinks' monitors"""
    out = subprocess.run(["pactl", "--format=json", "list", "sources"], check=True,
                         capture_output=True, text=True).stdout
    return [s for s in json.loads(out)
            if not s["name"].endswith(".monitor")
            and s.get("properties", {}).get("device.class") != "monitor"
            and str(s.get("monitor_of_sink", "n/a")) in ("n/a", "4294967295", "-1", "None")]


def _ports(source: dict[str, Any]) -> list[dict[str, Any]]:
    ports = source.get("ports", [])
    if isinstance(ports, dict):         # older pactl: a table keyed by name
        return [dict(p, name=p.get("name", k)) for k, p in ports.items()]
    return ports


def _line_ports(source: dict[str, Any]) -> list[str]:
    return [p["name"] for p in _ports(source) if "line" in p["name"].lower()]


def describe(items: Iterable[dict[str, Any]]) -> str:
    lines = []
    for s in items:
        lines.append(f"{s['name']}\n    {s.get('description', '')}")
        lines += [f"    port {p['name']}: {p.get('description', '')} "
                  f"({p.get('availability', 'unknown')})" for p in _ports(s)]
    return "\n".join(lines)


def choose(items: list[dict[str, Any]], name: str | None,
           port: str | None) -> tuple[dict[str, Any], str | None]:
    """The source called NAME, else the only one with a line port; and PORT,
    else that source's only line port"""
    if name:
        match = [s for s in items if s["name"] == name]
    else:
        match = [s for s in items if _line_ports(s)] or items
    if len(match) != 1:
        raise CaptureError("name the source ([linein] source or --source):\n"
                           + describe(items))
    source = match[0]
    names = [p["name"] for p in _ports(source)]
    if port is None:
        line = _line_ports(source)
        port = line[0] if len(line) == 1 else None
    elif port not in names:
        raise CaptureError(f"{source['name']} has no port {port}: {', '.join(names)}")
    return source, port


def unity(source: dict[str, Any]) -> int | None:
    """Puts SOURCE at its base volume (unity gain); returns that volume"""
    base = source.get("base_volume", {}).get("value")
    if base:
        subprocess.run(["pactl", "set-source-volume", source["name"], str(base)], check=True)
    return base


def parec(source: str, rate: int = RATE, latency_ms: int | None = None) -> Iterator[bytes]:
    """SOURCE as s16le stereo, one second at a time. Five seconds without
    data mean the audio server is stuck, not that the room is quiet: parec
    delivers silence as samples."""
    cmd = ["parec", "--raw", "--format=s16le", f"--rate={rate}", f"--channels={CHANNELS}",
           f"--device={source}", "--client-name=auto-rockbox"]
    if latency_ms:
        cmd.append(f"--latency-msec={latency_ms}")
    second = rate * CHANNELS * 2
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    if proc.stdout is None:
        raise CaptureError("parec gave no output pipe")
    out = proc.stdout
    sel = selectors.DefaultSelector()
    sel.register(out, selectors.EVENT_READ)
    pending = bytearray()
    try:
        while True:
            if not sel.select(timeout=5):
                raise CaptureError("no audio for 5 s: is the desktop audio server running?")
            chunk = os.read(out.fileno(), 65536)
            if not chunk:
                raise CaptureError(f"parec exited ({proc.poll()})")
            pending += chunk
            while len(pending) >= second:
                yield bytes(pending[:second])
                del pending[:second]
    finally:
        sel.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        out.close()


# ---------------------------------------------------------------- levels

def _dbfs(v: float) -> float | None:
    return 20 * math.log10(v / 32768) if v else None


@dataclass(frozen=True)
class Level:
    """One channel's level over a second, in 16-bit sample units"""

    peak: int
    rms: float
    clipped: int

    @property
    def peak_dbfs(self) -> float | None:
        return _dbfs(self.peak)

    @property
    def rms_dbfs(self) -> float | None:
        return _dbfs(self.rms)

    def as_json(self) -> dict[str, Any]:
        p, r = self.peak_dbfs, self.rms_dbfs
        return {"peak": self.peak, "rms": round(self.rms, 3),
                "peak_dbfs": None if p is None else round(p, 2),
                "rms_dbfs": None if r is None else round(r, 2),
                "clipped_samples": self.clipped}


def levels(raw: bytes, channels: int = CHANNELS) -> list[Level]:
    a = np.frombuffer(raw, "<i2").reshape(-1, channels).astype(np.int32)
    peak = np.abs(a).max(axis=0)
    rms = np.sqrt((a.astype(np.float64) ** 2).mean(axis=0))
    clipped = (np.abs(a) >= 32767).sum(axis=0)
    return [Level(int(p), float(r), int(c)) for p, r, c in zip(peak, rms, clipped, strict=True)]


def level_row(when: datetime, seq: int, lv: Sequence[Level]) -> str:
    """A level-log line, "utc,second,L rms,L peak,R rms,R peak" in dBFS with
    empty fields for digital silence: what a battery run's end is read from"""
    cols = [when.isoformat(), str(seq)]
    for c in lv:
        cols += ["" if v is None else f"{v:.2f}" for v in (c.rms_dbfs, c.peak_dbfs)]
    return ",".join(cols) + "\n"


def encode_wav(raw: bytes, rate: int = RATE, channels: int = CHANNELS) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw)
    return buf.getvalue()


# ---------------------------------------------------------------- publishing

class Publisher:
    """Writes each second into the ring and as the latest one. The JSON keeps
    the field names of the earlier capture script, which other tools read."""

    def __init__(self, directory: Path, history: int, source: str, port: str | None,
                 level_log: Path | None = None) -> None:
        self.dir = directory
        self.ring = Ring(directory, history)
        self.source = source
        self.port = port
        self.level_log = level_log

    def publish(self, seq: int, raw: bytes, published: datetime, elapsed: float) -> None:
        lv = levels(raw)
        meta = {"sequence": seq, "source": self.source, "port": self.port, "rate": RATE,
                "channels": CHANNELS, "sample_bits": 16, "frames": len(raw) // (2 * CHANNELS),
                "stream_offset_seconds": seq - 1, "published_utc": published.isoformat(),
                "elapsed_monotonic_seconds": round(elapsed, 3),
                "timestamp_scope": "host publication; audio-server latency is unmeasured",
                "levels": [c.as_json() for c in lv], "history_slot": self.ring.index(seq)}
        wav = encode_wav(raw)
        js = (json.dumps(meta, indent=2) + "\n").encode()
        if self.level_log:
            with self.level_log.open("a") as f:
                f.write(level_row(published, seq, lv))
        self.ring.put(seq, {"audio.wav": wav, "audio.json": js})
        atomic_write(self.dir / "latest.wav", wav)
        atomic_write(self.dir / "latest.json", js)


def serve(directory: Path, history: int, *, source: str | None = None,
          port: str | None = None, level_log: Path | None = None,
          echo: Callable[[str], object] = print) -> None:
    """Records and publishes until interrupted"""
    src, port = choose(sources(), source, port)
    if port:
        subprocess.run(["pactl", "set-source-port", src["name"], port], check=True)
    base = unity(src)
    echo(f"recording {src['name']} (port {port}, volume {base}: unity)")
    with exclusive(directory):
        pub = Publisher(directory, history, src["name"], port, level_log)
        t0 = time.monotonic()
        for seq, raw in enumerate(parec(src["name"]), 1):
            pub.publish(seq, raw, datetime.now(UTC), time.monotonic() - t0)
            if seq == 1:
                echo(f"first second in {directory / 'latest.wav'}; keeping {history} s")


# ---------------------------------------------------------------- reading

@dataclass(frozen=True)
class Second:
    seq: int
    published: datetime         # host publication time, UTC
    wav: Path

    @property
    def stamp(self) -> str:
        """HH:MM:SS.mmm: to the ms, as two seconds can share a wall second"""
        return self.published.strftime("%H:%M:%S.%f")[:12]

    def __str__(self) -> str:
        return f"#{self.seq} {self.stamp}"

    def samples(self) -> tuple[np.ndarray, int]:
        """Frames (n, channels) in full-scale units, and the sample rate"""
        with wave.open(str(self.wav)) as w:
            rate, ch = w.getframerate(), w.getnchannels()
            a = np.frombuffer(w.readframes(w.getnframes()), "<i2").reshape(-1, ch)
        return a.astype(np.float64) / 32768, rate


def history(directory: Path) -> list[Second]:
    """Every second in the ring, oldest first"""
    out = []
    for js in directory.glob("history/*/audio.json"):
        try:
            m = json.loads(js.read_text())
            out.append(Second(int(m["sequence"]), datetime.fromisoformat(m["published_utc"]),
                              js.with_name("audio.wav")))
        except (OSError, ValueError, KeyError, TypeError):
            continue                    # a slot being rewritten
    out.sort(key=lambda s: s.published)
    return out


def parse_when(text: str, ref: datetime) -> datetime:
    """A UTC time: ISO 8601 with a date, or HH:MM:SS[.fff], taken as the
    occurrence within 12 h of REF"""
    try:
        t = datetime.fromisoformat(text)
        return t if t.tzinfo else t.replace(tzinfo=UTC)
    except ValueError:
        pass
    d = datetime.combine(ref.date(), clock_time.fromisoformat(text), UTC)
    if d > ref + timedelta(hours=12):
        d -= timedelta(days=1)
    elif d < ref - timedelta(hours=12):
        d += timedelta(days=1)
    return d


def between(seconds: Iterable[Second], since: datetime, until: datetime) -> list[Second]:
    return [s for s in seconds if since <= s.published <= until]


def by_seq(seconds: Iterable[Second], first: int, last: int) -> list[Second]:
    """Seconds FIRST..LAST; where an earlier run left the same number, the newest"""
    newest = {s.seq: s for s in seconds if first <= s.seq <= last}
    return sorted(newest.values(), key=lambda s: s.published)


def select(seconds: Sequence[Second], window: Sequence[str] = (),
           seq: Sequence[int] | None = None) -> list[Second]:
    """What a command line names: SEQ (first and last sequence number), or
    WINDOW: nothing for the last 60 s, N for the last N, FROM TO for a UTC
    window (HH:MM:SS[.fff] or ISO)"""
    if seq:
        return by_seq(seconds, seq[0], seq[-1])
    if len(window) == 2:
        since = parse_when(window[0], datetime.now(UTC))
        until = parse_when(window[1], since)
        if until < since:
            until += timedelta(days=1)
        return between(seconds, since, until)
    if len(window) > 2:
        raise ValueError(f"{len(window)} window arguments")
    n = int(window[0]) if window else LAST_DEFAULT
    return list(seconds[-n:]) if n > 0 else []


def gaps_in(seconds: Sequence[Second]) -> list[tuple[int, int]]:
    """(last, next) sequence numbers where SECONDS skip some: the ring
    overwrote them, or the publisher restarted"""
    return [(a.seq, b.seq) for a, b in itertools.pairwise(seconds) if b.seq != a.seq + 1]


# ---------------------------------------------------------------- analysis

def _spectrum(x: np.ndarray, rate: int, pad: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(x)
    mag = np.abs(np.fft.rfft(x * np.hanning(n), n * pad))
    return np.fft.rfftfreq(n * pad, 1 / rate), mag


def dominant_hz(x: np.ndarray, rate: int, pad: int = 1) -> float:
    """The strongest frequency above MIN_HZ (below it: hum and DC offset)"""
    f, mag = _spectrum(x - x.mean(), rate, pad)
    lo = int(np.searchsorted(f, MIN_HZ))
    return float(f[lo + int(np.argmax(mag[lo:]))])


def tone_kind(left_hz: float, right_hz: float) -> Kind:
    """"stereo": the test tone as made (1 kHz left, 1.5 kHz right); "tone":
    the tones on other channels (mono or 1 kHz-only files); else "music\""""
    def near(hz: float, t: float) -> bool:
        return abs(hz - t) < TONE_TOLERANCE_HZ
    if near(left_hz, TONES[0]) and near(right_hz, TONES[1]):
        return "stereo"
    if all(any(near(hz, t) for t in TONES) for hz in (left_hz, right_hz)):
        return "tone"
    return "music"


@dataclass(frozen=True)
class Channel:
    db: float           # RMS, dBFS (a full-scale square wave is 0)
    hz: float           # the dominant frequency
    purity: float       # share of the power within 10 Hz of it

    def __str__(self) -> str:
        return f"{self.db:6.1f} dB {self.hz:8.1f} Hz {self.purity * 100:3.0f}%"


def analyse(x: np.ndarray, rate: int) -> Channel:
    """One channel of a second: zero-padded 4x, so the peak sits within 0.25 Hz"""
    x = x - x.mean()
    f, mag = _spectrum(x, rate, 4)
    lo = int(np.searchsorted(f, MIN_HZ))
    k = lo + int(np.argmax(mag[lo:]))
    w = round(10 / f[1])
    p = mag ** 2
    purity = p[max(0, k - w):k + w + 1].sum() / (p.sum() + 1e-30)
    return Channel(float(20 * np.log10(x.std() + 1e-12)), float(f[k]), float(purity))


def low_runs(x: np.ndarray, rate: int) -> list[tuple[int, int]]:
    """Dropouts in a steady signal: runs of 1 ms windows below half the
    second's median RMS, as (start ms, length ms)"""
    n = rate // 1000
    x = x - x.mean()
    r = np.sqrt((x[:len(x) // n * n].reshape(-1, n) ** 2).mean(axis=1))
    low = np.concatenate(([0], (r < np.median(r) / 2).astype(np.int8), [0]))
    edges = np.flatnonzero(np.diff(low))
    starts, ends = edges[0::2], edges[1::2]
    return list(zip(starts.tolist(), (ends - starts).tolist(), strict=True))


@dataclass(frozen=True)
class Heard:
    """A second as analysed: each channel's level and tone, the left one's dropouts"""

    second: Second
    channels: tuple[Channel, ...]
    low: tuple[tuple[int, int], ...]

    @property
    def low_ms(self) -> int:
        return sum(n for _, n in self.low)

    @property
    def loud(self) -> bool:
        return self.channels[0].db >= SILENT_DB

    def is_tone(self, stereo: bool = True) -> bool:
        """The test tone, clean on both channels (at any level); with STEREO
        false, either tone on either channel"""
        if min(c.purity for c in self.channels) < CLEAN:
            return False
        kind = tone_kind(self.channels[0].hz, self.channels[-1].hz)
        return kind == "stereo" if stereo else kind != "music"

    def levels_line(self) -> str:
        return f"{self.second}  " + " | ".join(map(str, self.channels))

    def low_line(self) -> str:
        return f"{self.second}  {self.low_ms:4d} ms low {list(self.low[:12])}"


def hear(second: Second) -> Heard:
    x, rate = second.samples()
    return Heard(second, tuple(analyse(x[:, c], rate) for c in range(x.shape[1])),
                 tuple(low_runs(x[:, 0], rate)))


@dataclass
class ToneCheck:
    heard: list[Heard]
    need: int
    tone: list[Heard] = field(default_factory=list)      # seconds with the tone
    dropouts: list[Heard] = field(default_factory=list)  # whole tone seconds with gaps

    @property
    def problems(self) -> list[str]:
        out = [] if len(self.tone) >= self.need else [f"tone seen in {len(self.tone)} s only"]
        return out + [f"dropout {h.low_line()}" for h in self.dropouts]

    @property
    def note(self) -> str:
        return f"{len(self.tone)} tone s"

    def report(self) -> str:
        """Every second of the window: levels, tone, dropouts"""
        return "\n".join(f"{h.levels_line()}  {h.low_ms:4d} ms low" for h in self.heard)


def judge(heard: Sequence[Heard], *, need: int = 6, stereo: bool = True,
          span: int | None = None) -> ToneCheck:
    """Tone seconds and the dropouts inside them. A tone second is clean and
    within FULL_DB of the loudest (the step-down is not; clips are mastered
    at different levels, so no fixed floor). A second at either end of a run
    of tone (consecutive sequence numbers) is partial, the tone starting or
    stopping in it, so only whole ones count; with SPAN, only those among
    the first SPAN tone seconds (what follows may be another file)."""
    clean = [h for h in heard if h.is_tone(stereo)]
    top = max((h.channels[0].db for h in clean), default=0.0)
    tone = [h for h in clean if h.channels[0].db >= top - FULL_DB]
    seqs = {h.second.seq for h in tone}
    whole = [h for h in tone[:span]
             if h.second.seq - 1 in seqs and h.second.seq + 1 in seqs]
    return ToneCheck(list(heard), need, tone, [h for h in whole if h.low_ms > DROPOUT_MS])


def tone_check(directory: Path, since: datetime, until: datetime, *, need: int = 6,
               stereo: bool = True, span: int | None = None,
               lag: float = LAG_S) -> ToneCheck:
    """Whether the player played the test tone whole between SINCE and UNTIL
    (this PC's clock, e.g. a step's start and end): the seconds stamped LAG
    later, judged as judge() does. SINCE is best after the player's boot, so
    a tone left over from the step before is outside the window."""
    shift = timedelta(seconds=lag)
    window = between(history(directory), since + shift, until + shift)
    return judge([hear(s) for s in window], need=need, stereo=stereo, span=span)
