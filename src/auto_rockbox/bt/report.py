"""`arb bt report`: a range run's files merged into one timeline and a
summary: how long the stream was silent and in what sizes of gap, when it
first stuttered, each drop with the time to the re-page, to the link and to
audio, the RSSI both sides saw, the player's bitpool and buffer waits, and
the media flow the PC received.

A drop is reported by whichever files saw it (the player's log, BlueZ's
connection log, the HCI trace, the gap log losing its node); reports within
MERGE_S of each other are one drop. Audio is back where the silence around
the drop ends in the gap log (what a listener hears); without a gap log, at
the first media packet in the PC's trace, else at the player's "streaming".
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from . import bluez, flow, rundir
from .gaps import logged_min_ms
from .rundir import clock

ROW_S = 10              # one row of the continuous series per this many seconds
MERGE_S = 15.0          # drop reports this close together are one drop
STUTTER_MS = 100        # the first gap this long is the first stutter
EVENT_GAP_MS = 500      # gaps this long are timeline events; shorter ones only count
EVENT_STALL_S = 1.0     # media stalls this long likewise
JOIN_S = 0.05           # silences this close together are one
GAP_SIZES = ((100, "< 0.1 s"), (300, "0.1-0.3 s"), (1000, "0.3-1 s"), (5000, "1-5 s"),
             (None, ">= 5 s"))

_DROP = re.compile(r"bt: disconnected: ([0-9a-f]{2})")
_PLAYER_EVENTS = (
    ("drop", _DROP),
    ("page", re.compile(r"bt: link lost: paging again")),
    ("link", re.compile(r"bt: connected, handle ")),
    ("stream", re.compile(r"bt: streaming, ")),
    ("stall", re.compile(r"STALL ")),
    ("role", re.compile(r"bt: role change ")),
)
_MEDIA = re.compile(r"bt: media \d+ pkts bp (\d+) behind (-?\d+) ms waits (\d+) restarts (\d+)")
_LINK = re.compile(r"bt link \d+: (\w+) rssi (-?\d+) dBm txpwr (\d+)")
WALK_NOTE = "walk: "


@dataclass(frozen=True)
class Media:
    bitpool: int
    behind_ms: int
    waits: int
    restarts: int


@dataclass
class PlayerLog:
    """What the player's lines say: link events, 10 s media stats, link polls"""

    events: list[tuple[datetime | None, str, str]] = field(default_factory=list)
    media: list[tuple[datetime | None, Media]] = field(default_factory=list)
    rssi: list[tuple[datetime | None, int, int]] = field(default_factory=list)   # rssi, txpwr
    notes: list[tuple[datetime, str]] = field(default_factory=list)              # walk: ...
    first: datetime | None = None   # the first and last stamped line's time
    last: datetime | None = None

    def add(self, at: datetime | None, line: str) -> None:
        if at is not None:
            self.first, self.last = self.first or at, at
        if line.startswith(WALK_NOTE) and at is not None:
            self.notes.append((at, line.removeprefix(WALK_NOTE)))
        elif m := _MEDIA.match(line):
            self.media.append((at, Media(*map(int, m.groups()))))
        elif m := _LINK.match(line):
            self.rssi.append((at, int(m.group(2)), int(m.group(3))))
        else:
            for kind, rx in _PLAYER_EVENTS:
                if rx.match(line):
                    self.events.append((at, kind, line))
                    break

    def summary(self, label: str) -> list[str]:
        out = []
        drops = Counter(m.group(1) for _, k, t in self.events
                        if k == "drop" and (m := _DROP.match(t)))
        if drops:
            pages = sum(k == "page" for _, k, _ in self.events)
            out.append(f"{label}: drops " + ", ".join(f"0x{r} x{n}" for r, n in sorted(
                drops.items())) + f"; re-pages {pages}")
        if self.media:
            bp = [m.bitpool for _, m in self.media]
            out.append(f"{label}: bitpool {min(bp)}-{max(bp)} (median "
                       f"{statistics.median(bp):g}), ACL buffer waits "
                       f"{sum(m.waits for _, m in self.media)}, clock restarts "
                       f"{sum(m.restarts for _, m in self.media)}, behind up to "
                       f"{max(m.behind_ms for _, m in self.media)} ms "
                       f"({len(self.media)} stats lines)")
        if self.rssi:
            r = [x for _, x, _ in self.rssi]
            out.append(f"{label}: link rssi {min(r)} to {max(r)} dBm, median "
                       f"{statistics.median(r):g} (txpwr {self.rssi[-1][2]}, "
                       f"{len(r)} polls)")
        return out


def _join(iv: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    out: list[tuple[datetime, datetime]] = []
    for a, b in sorted(iv):
        if out and a - out[-1][1] <= timedelta(seconds=JOIN_S):
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


@dataclass
class GapLog:
    """A gap log within the report's window. The time it covers and its
    silences (the gaps, and the times the stream was lost) are clipped to
    the window rather than dropped, so a loss that began before the window
    still counts inside it."""

    gaps: list[tuple[datetime, int]]                # start, ms
    lost: list[datetime]
    nodes: list[datetime]
    span: tuple[datetime, datetime] | None
    silences: list[tuple[datetime, datetime]]       # joined where they touch
    min_ms: int | None

    @classmethod
    def read(cls, path: Path, lo: datetime | None, hi: datetime | None) -> GapLog:
        gaps: list[tuple[datetime, int]] = []
        levels: list[datetime] = []
        nodes: list[datetime] = []
        lost: list[tuple[datetime, datetime | None]] = []
        for at, text in rundir.read(path):
            word, _, rest = text.partition(" ")
            if word == "gap":
                gaps.append((at, int(rest.split()[0])))
            elif word == "level":
                levels.append(at)
            elif word == "lost":
                lost.append((at, None))
            elif word == "node":
                nodes.append(at)
                if lost and lost[-1][1] is None:
                    lost[-1] = (lost[-1][0], at)
        span: tuple[datetime, datetime] | None = None
        silences: list[tuple[datetime, datetime]] = []
        if starts := levels + nodes + [t for t, _ in gaps]:
            end = max([t + timedelta(seconds=1) for t in levels]
                      + [t + timedelta(milliseconds=ms) for t, ms in gaps]
                      + [a for a, _ in lost] + starts)
            a = max(min(starts), lo) if lo else min(starts)
            b = min(end, hi) if hi else end
            if a < b:
                span = (a, b)
                iv = [(t, t + timedelta(milliseconds=ms)) for t, ms in gaps]
                iv += [(x, y or end) for x, y in lost]
                silences = [(max(x, a), min(y, b)) for x, y in _join(iv) if x < b and y > a]

        def inside(t: datetime) -> bool:
            return (lo is None or t >= lo) and (hi is None or t < hi)

        return cls([g for g in gaps if inside(g[0])], [t for t, _ in lost if inside(t)],
                   [t for t in nodes if inside(t)], span, silences,
                   logged_min_ms(path.read_text(errors="replace")))


@dataclass
class Drop:
    at: datetime
    reason: int | None
    sources: set[str]
    paged: datetime | None = None
    link: datetime | None = None
    audio: datetime | None = None

    def line(self) -> str:
        why = flow.reason_text(self.reason) if self.reason is not None else "drop"
        times = ", ".join(f"{name} +{(t - self.at).total_seconds():.1f} s" if t else f"no {name}"
                          for name, t in (("re-paged", self.paged), ("link", self.link),
                                          ("audio", self.audio)))
        return f"{clock(self.at)} {why} [{', '.join(sorted(self.sources))}]: {times}"


@dataclass(frozen=True)
class Entry:
    at: datetime
    source: str
    text: str

    def line(self) -> str:
        return f"{clock(self.at)}  {self.source:6} {self.text}"


def _median(xs: Iterable[float]) -> float | None:
    xs = list(xs)
    return statistics.median(xs) if xs else None


def _clip(iv: list[tuple[datetime, datetime]], lo: datetime, hi: datetime) -> float:
    """Seconds of IV inside [LO, HI)"""
    return sum(max(0.0, (min(b, hi) - max(a, lo)).total_seconds()) for a, b in iv)


@dataclass
class Run:
    """A run directory's files, each read if present, within [lo, hi)"""

    path: Path
    lo: datetime | None = None
    hi: datetime | None = None
    player: PlayerLog = field(default_factory=PlayerLog)
    card: PlayerLog | None = None
    gaps: GapLog | None = None
    conn: list[tuple[datetime, str]] = field(default_factory=list)
    rssi: list[tuple[datetime, bluez.ConnInfo | None]] = field(default_factory=list)
    trace: flow.Flow | None = None     # pc.btsnoop's media flow
    files: list[str] = field(default_factory=list)

    def inside(self, t: datetime) -> bool:
        return (self.lo is None or t >= self.lo) and (self.hi is None or t < self.hi)

    @classmethod
    def read(cls, path: Path, lo: datetime | None = None, hi: datetime | None = None) -> Run:
        r = cls(path, lo, hi)
        if (p := path / rundir.WALK).exists():
            r.files.append(p.name)
            for at, text in rundir.read(p):
                if r.inside(at):
                    r.player.add(at, text)
        if (p := path / rundir.CARD_LOG).exists():
            r.files.append(p.name)
            r.card = PlayerLog()
            for line in p.read_text(errors="replace").splitlines():
                r.card.add(None, line)
        if (p := path / rundir.GAPS).exists():
            r.files.append(p.name)
            r.gaps = GapLog.read(p, lo, hi)
        if (p := path / rundir.CONN).exists():
            r.files.append(p.name)
            r.conn = [(t, x) for t, x in rundir.read(p) if r.inside(t)]
        if (p := path / rundir.RSSI).exists():
            r.files.append(p.name)
            r.rssi = [(t, bluez.parse_rssi_line(x)) for t, x in rundir.read(p) if r.inside(t)]
        if (p := path / rundir.TRACE).exists():
            r.files.append(p.name)
            r.trace = flow.read(p, lo.timestamp() if lo else None, hi.timestamp() if hi else None)
        return r

    # ------------------------------------------------------------ drops

    def drops(self) -> list[Drop]:
        seen: list[tuple[datetime, int | None, str]] = []
        seen += [(t, int(m.group(1), 16), "player") for t, k, x in self.player.events
                 if t and k == "drop" and (m := _DROP.match(x))]
        seen += [(t, None, "conn") for t, x in self.conn if x == "down"]
        if self.gaps:
            seen += [(t, None, "gaps") for t in self.gaps.lost]
        if self.trace:
            media = self.trace.handles
            seen += [(flow.when(e.t), e.code, "hci") for e in self.trace.events
                     if e.kind == "disconnect" and (not media or e.handle in media)]
        drops: list[Drop] = []
        for t, reason, src in sorted(seen, key=lambda x: x[0]):
            if drops and t - drops[-1].at <= timedelta(seconds=MERGE_S):
                drops[-1].sources.add(src)
                if drops[-1].reason is None:
                    drops[-1].reason = reason
            else:
                drops.append(Drop(t, reason, {src}))
        for i, d in enumerate(drops):
            self._recovery(d, drops[i + 1].at if i + 1 < len(drops) else None)
        return drops

    def _recovery(self, d: Drop, until: datetime | None) -> None:
        def first(ts: Iterable[datetime], after: datetime) -> datetime | None:
            return min((t for t in ts if t >= after and (until is None or t < until)),
                       default=None)

        ev = self.trace.events if self.trace else []
        d.paged = first([t for t, k, _ in self.player.events if t and k == "page"]
                        + [t for t, x in self.conn if x == "page"]
                        + [flow.when(e.t) for e in ev if e.kind == "page"], d.at)
        d.link = first([t for t, k, _ in self.player.events if t and k == "link"]
                       + [t for t, x in self.conn if x in ("back", "up")]
                       + [flow.when(e.t) for e in ev if e.kind == "connect" and e.code == 0]
                       + (self.gaps.nodes if self.gaps else []), d.at)
        if self.gaps and self.gaps.span:
            # The drop's silence: it may start before the drop (a stall) or
            # a little after (the node goes once BlueZ notices)
            edge = d.at + timedelta(seconds=MERGE_S)
            ends = [b for a, b in self.gaps.silences if a <= edge and b >= d.at]
            d.audio = first([max(ends)] if ends else [], d.at)
        elif self.trace and d.link:
            d.audio = first([flow.when(p.t) for p in self.trace.packets], d.link)
        elif d.link:
            d.audio = first([t for t, k, _ in self.player.events if t and k == "stream"],
                            d.link)

    # ------------------------------------------------------------ summary

    def span(self) -> tuple[datetime, datetime] | None:
        ts = [t for t in (self.player.first, self.player.last) if t]
        ts += [t for t, _ in self.conn] + [t for t, _ in self.rssi]
        if self.gaps and (s := self.gaps.span):
            ts += list(s)
        if self.trace and self.trace.fragments:
            ts += [flow.when(self.trace.fragments[0].t), flow.when(self.trace.fragments[-1].t)]
        return (min(ts), max(ts)) if ts else None

    def summary(self) -> list[str]:
        out = [f"run {self.path}: {', '.join(self.files) or 'no run files'}"]
        if span := self.span():
            out.append(f"from {clock(span[0])} to {clock(span[1])} "
                       f"({(span[1] - span[0]).total_seconds():.1f} s)")
        out += self._silence()
        drops = self.drops()
        if drops:
            kinds = Counter(flow.reason_text(d.reason) if d.reason is not None else "unknown"
                            for d in drops)
            out.append(f"drops: {len(drops)} (" + ", ".join(
                f"{k} x{n}" for k, n in sorted(kinds.items())) + ")")
            out += [f"  {d.line()}" for d in drops]
        readings = [c for _, c in self.rssi if c]
        if self.rssi:
            if readings:
                r = [c.rssi for c in readings]
                tx = [c.tx_power for c in readings]
                out.append(f"pc rssi: min {min(r)}, median {statistics.median(r):g}, mean "
                           f"{statistics.fmean(r):.1f} over {len(r)} readings "
                           f"({len(self.rssi) - len(r)} without a link); tx power "
                           f"{min(tx)} to {max(tx)}")
            else:
                out.append(f"pc rssi: no link in {len(self.rssi)} readings")
        out += self.player.summary("player")
        if self.card:
            out += self.card.summary("card log")
        if self.trace:
            stalls = self.trace.stalls()
            frames = Counter(p.frames for p in self.trace.packets).most_common(1)
            out.append(f"media flow: {flow.kbps(self.trace):.1f} KB/s, {len(stalls)} stalls > "
                       f"{flow.STALL_S * 1000:.0f} ms ({sum(s.seconds for s in stalls):.1f} s), "
                       f"RTP sequence skips {self.trace.skipped}"
                       + (f", mostly {frames[0][0]} SBC frames per packet" if frames else ""))
        return out

    def _silence(self) -> list[str]:
        g = self.gaps
        if g is None or (span := g.span) is None:
            return []
        silent = _clip(g.silences, *span)
        least = f" >= {g.min_ms} ms" if g.min_ms else ""
        lost = f"; the stream lost: {len(g.lost)}" if g.lost else ""
        out = [f"silent: {silent:.1f} s of {(span[1] - span[0]).total_seconds():.1f} s "
               f"({len(g.gaps)} gaps{least}{lost})"]
        sizes = Counter(next(label for top, label in GAP_SIZES if top is None or ms < top)
                        for _, ms in g.gaps)
        out.append("gaps: " + ", ".join(f"{label} {sizes[label]}" for _, label in GAP_SIZES))
        if stutter := next((t for t, ms in g.gaps if ms >= STUTTER_MS), None):
            out.append(f"first stutter (a gap >= {STUTTER_MS} ms): {clock(stutter)}, "
                       f"{(stutter - span[0]).total_seconds():.1f} s into the gap log")
        return out

    # ------------------------------------------------------------ timeline

    def timeline(self) -> list[Entry]:
        e = [Entry(t, "player", x) for t, k, x in self.player.events if t]
        e += [Entry(t, "walk", x) for t, x in self.player.notes]
        e += [Entry(t, "conn", x) for t, x in self.conn]
        if self.gaps:
            e += [Entry(t, "gaps", f"gap {ms} ms") for t, ms in self.gaps.gaps
                  if ms >= EVENT_GAP_MS]
            e += [Entry(t, "gaps", "stream lost") for t in self.gaps.lost]
            e += [Entry(t, "gaps", "stream") for t in self.gaps.nodes]
        if self.trace:
            e += [Entry(flow.when(x.t), "hci", x.text) for x in self.trace.events]
            e += [Entry(flow.when(s.t), "flow", f"media stall {s.seconds:.2f} s")
                  for s in self.trace.stalls(EVENT_STALL_S)]
        e += self._rows()
        return sorted(e, key=lambda x: x.at)

    def _rows(self) -> list[Entry]:
        """The continuous series (silence, RSSI, throughput, bitpool), one
        row per ROW_S that has any of them"""
        span = self.span()
        if span is None:
            return []
        t0 = span[0].replace(microsecond=0) - timedelta(seconds=span[0].second % ROW_S)

        def row(t: datetime | float) -> int:
            s = t if isinstance(t, float | int) else t.timestamp()
            return int((s - t0.timestamp()) // ROW_S)

        def start(i: int) -> datetime:
            return t0 + timedelta(seconds=i * ROW_S)

        silent: dict[int, float] = {}
        if self.gaps and (gs := self.gaps.span):
            for i in range(row(gs[0]), row(gs[1] - timedelta(microseconds=1)) + 1):
                silent[i] = _clip(self.gaps.silences, start(i), start(i + 1))
        pc: dict[int, list[bluez.ConnInfo | None]] = {}
        for at, c in self.rssi:
            pc.setdefault(row(at), []).append(c)
        kb: Counter[int] = Counter()
        stalls: Counter[int] = Counter()
        if self.trace:
            for s in self.trace.seconds():
                kb[row(s.t)] += s.size
            for st in self.trace.stalls():
                stalls[row(st.t)] += 1
        media: dict[int, list[Media]] = {}
        for at, m in self.player.media:
            if at:
                media.setdefault(row(at), []).append(m)
        link: dict[int, list[int]] = {}
        for at, r, _ in self.player.rssi:
            if at:
                link.setdefault(row(at), []).append(r)

        rows = []
        for i in sorted(set(silent) | set(pc) | set(kb) | set(media) | set(link)):
            parts = []
            if i in silent:
                parts.append(f"silent {silent[i]:4.1f} s")
            if i in pc:
                m = _median(c.rssi for c in pc[i] if c)
                parts.append(f"pc rssi {m:g}" if m is not None else "pc rssi none")
            if i in kb:
                parts.append(f"{kb[i] / ROW_S / 1000:4.1f} KB/s, {stalls[i]} stalls")
            if i in media:
                ms = media[i]
                parts.append(f"bp {min(m.bitpool for m in ms)} waits "
                             f"{sum(m.waits for m in ms)} restarts {sum(m.restarts for m in ms)}")
            if i in link:
                parts.append(f"player rssi {_median(link[i]):g}")
            rows.append(Entry(start(i), f"{ROW_S} s", " | ".join(parts)))
        return rows


def first_day(path: Path) -> date:
    """The date of a run's first stamp, for a window given as times of day"""
    for name in (rundir.WALK, rundir.GAPS, rundir.CONN, rundir.RSSI):
        if (p := path / name).exists():
            for at, _ in rundir.read(p):
                return at.date()
    if (p := path / rundir.TRACE).exists() and (t := flow.first_time(p)) is not None:
        return flow.when(t).date()
    return date.today()


def report(path: Path, lo: datetime | None = None, hi: datetime | None = None) -> list[str]:
    run = Run.read(path, lo, hi)
    return [*run.summary(), "", "timeline", *(e.line() for e in run.timeline())]
