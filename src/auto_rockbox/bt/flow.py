"""The A2DP media flow in this PC's HCI trace (`btmon -w`, BTSnoop monitor
format), as the PC received it from a player: ACL fragments, reassembled
media packets (RTP sequence, SBC frames), per-second throughput, and the
stalls between packets.

A link's margin shows here before it is audible. A source that cannot send
lowers its bitpool (smaller packets), the KB/s falls below what the bitpool
needs, and media packets arrive with growing stalls between them. The packet
shape tells the link's packet types: one ~430-byte media packet per 2-DH5
against ~1000 bytes split over three DH5 fragments.

A media packet is recognised by its content, not by the AVDTP set-up, so a
trace started mid-stream works: an L2CAP frame on a dynamic channel holding
an RTP version 2 header and an SBC payload (its sync word after the frame
count).
"""

from __future__ import annotations

import mmap
import struct
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..errors import ArbError
from .rundir import clock

MAGIC = b"btsnoop\0"
MONITOR = 2001                      # BTSnoop datalink of btmon's monitor format
EPOCH_US = 0x00DCDDB30F2F8000       # BTSnoop's microseconds at 1970-01-01 UTC
OP_COMMAND, OP_EVENT, OP_ACL_TX, OP_ACL_RX = 2, 3, 4, 5
STALL_S = 0.1                       # a longer wait between media packets is a stall

_RECORD = struct.Struct(">IIIIq")
_FIRST_DYNAMIC_CID = 0x40
_SBC_SYNC = 0x9C
_CREATE_CONNECTION = 0x0405         # HCI command opcode: the PC pages a device

REASONS = {
    0x04: "page timeout",
    0x05: "authentication failure",
    0x08: "connection timeout",
    0x13: "remote user terminated",
    0x14: "remote low resources",
    0x15: "remote power off",
    0x16: "local host terminated",
    0x22: "LMP response timeout",
}
RANGE_REASONS = frozenset({0x08, 0x22})     # lost to the radio, not ended by a side


def reason_text(code: int) -> str:
    name = REASONS.get(code)
    return f"0x{code:02x}" + (f" ({name})" if name else "")


class FlowError(ArbError):
    pass


@dataclass(frozen=True, slots=True)
class Record:
    t: float                        # Unix time
    index: int                      # the adapter (0 for hci0; 0xffff for system notes)
    op: int
    data: bytes


def records(path: Path) -> Iterator[Record]:
    """A trace's records in file order (btmon writes them as they happen)"""
    with path.open("rb") as f:
        if f.read(16)[:8] != MAGIC:
            raise FlowError(f"{path}: not a BTSnoop file")
        f.seek(12)
        (link,) = struct.unpack(">I", f.read(4))
        if link != MONITOR:
            raise FlowError(f"{path}: datalink {link}, not btmon's monitor format ({MONITOR})")
        with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as m:
            pos, end = 16, len(m)
            while pos + _RECORD.size <= end:
                _, size, flags, _, ts = _RECORD.unpack_from(m, pos)
                pos += _RECORD.size
                if pos + size > end:            # cut short: the capture was still running
                    return
                yield Record((ts - EPOCH_US) / 1e6, flags >> 16, flags & 0xFFFF,
                             m[pos:pos + size])
                pos += size


@dataclass(frozen=True, slots=True)
class Fragment:
    t: float
    handle: int
    size: int                       # ACL payload bytes


@dataclass(frozen=True, slots=True)
class Packet:
    """One media packet: an RTP packet on an AVDTP media channel"""

    t: float                        # its last fragment's arrival
    handle: int
    cid: int
    size: int                       # L2CAP payload bytes
    seq: int
    frames: int                     # SBC frames in it


@dataclass(frozen=True, slots=True)
class Event:
    """An HCI event or command that changes a link"""

    t: float
    kind: str                       # connect, disconnect, page, role, mode, slots, types
    text: str
    handle: int | None = None
    code: int | None = None         # a connect's status, a disconnect's reason


@dataclass(frozen=True, slots=True)
class Second:
    t: int                          # Unix second
    packets: int
    frames: int
    fragments: int
    size: int                       # ACL bytes on the media links


@dataclass(frozen=True, slots=True)
class Stall:
    t: float                        # the packet before it
    seconds: float
    seq_step: int


def _addr(b: bytes) -> str:
    return ":".join(f"{x:02X}" for x in reversed(b[:6]))


@dataclass
class Flow:
    fragments: list[Fragment] = field(default_factory=list)
    packets: list[Packet] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    skipped: int = 0                # RTP sequence numbers that never arrived

    @property
    def handles(self) -> set[int]:
        """The links that carried media"""
        return {p.handle for p in self.packets}

    def seconds(self) -> list[Second]:
        rows: dict[int, list[int]] = {}
        for p in self.packets:
            r = rows.setdefault(int(p.t), [0, 0, 0, 0])
            r[0] += 1
            r[1] += p.frames
        media = self.handles
        for f in self.fragments:
            if f.handle in media:
                r = rows.setdefault(int(f.t), [0, 0, 0, 0])
                r[2] += 1
                r[3] += f.size
        return [Second(t, *r) for t, r in sorted(rows.items())]

    def stalls(self, longer_than: float = STALL_S) -> list[Stall]:
        out = []
        for a, b in zip(self.packets, self.packets[1:], strict=False):
            if b.t - a.t > longer_than:
                out.append(Stall(a.t, b.t - a.t, (b.seq - a.seq) & 0xFFFF))
        return out


class _Reader:
    """Reassembles L2CAP frames per ACL link and keeps what Flow holds"""

    def __init__(self) -> None:
        self.flow = Flow()
        self.partial: dict[int, tuple[float, bytearray]] = {}
        self.last_seq: dict[tuple[int, int], int] = {}
        self.addrs: dict[int, str] = {}

    def record(self, r: Record) -> None:
        if r.op == OP_ACL_RX and len(r.data) >= 4:
            self._acl(r.t, r.data)
        elif r.op == OP_EVENT and len(r.data) >= 2:
            self._event(r.t, r.data[0], r.data[2:])
        elif (r.op == OP_COMMAND and len(r.data) >= 9
              and struct.unpack_from("<H", r.data)[0] == _CREATE_CONNECTION):
            self.flow.events.append(Event(r.t, "page", f"page {_addr(r.data[3:9])}"))

    def _acl(self, t: float, d: bytes) -> None:
        hf, size = struct.unpack_from("<HH", d)
        handle, boundary = hf & 0xFFF, (hf >> 12) & 3
        self.flow.fragments.append(Fragment(t, handle, size))
        if boundary in (0, 2):                  # a first fragment
            self.partial[handle] = (t, bytearray(d[4:]))
        elif handle in self.partial:
            self.partial[handle][1].extend(d[4:])
        else:
            return
        buf = self.partial[handle][1]
        if len(buf) < 4:
            return
        length, cid = struct.unpack_from("<HH", buf)
        if len(buf) < length + 4:
            return
        del self.partial[handle]
        pl = buf[4:4 + length]
        if (cid < _FIRST_DYNAMIC_CID or len(pl) < 14 or pl[0] & 0xCF != 0x80
                or pl[13] != _SBC_SYNC):
            return
        seq = struct.unpack_from(">H", pl, 2)[0]
        key = (handle, cid)
        if key in self.last_seq:
            step = (seq - self.last_seq[key]) & 0xFFFF
            if 1 < step < 0x8000:
                self.flow.skipped += step - 1
        self.last_seq[key] = seq
        self.flow.packets.append(Packet(t, handle, cid, length, seq, pl[12] & 0x0F))

    def _link(self, handle: int) -> str:
        a = self.addrs.get(handle)
        return f"handle {handle}" + (f" ({a})" if a else "")

    def _event(self, t: float, code: int, p: bytes) -> None:
        ev = self.flow.events
        if code == 0x03 and len(p) >= 9:       # Connection Complete
            status, handle = p[0], struct.unpack_from("<H", p, 1)[0]
            if status == 0:
                self.addrs[handle] = _addr(p[3:9])
                ev.append(Event(t, "connect", f"connect {_addr(p[3:9])} handle {handle}",
                                handle, 0))
            else:
                ev.append(Event(t, "connect", f"connect {_addr(p[3:9])} failed: "
                                f"{reason_text(status)}", None, status))
        elif code == 0x05 and len(p) >= 4:     # Disconnection Complete
            handle, reason = struct.unpack_from("<H", p, 1)[0], p[3]
            ev.append(Event(t, "disconnect", f"disconnect {self._link(handle)}: "
                            f"{reason_text(reason)}", handle, reason))
            self.partial.pop(handle, None)
            for key in [k for k in self.last_seq if k[0] == handle]:
                del self.last_seq[key]
        elif code == 0x12 and len(p) >= 8:     # Role Change
            role = "central" if p[7] == 0 else "peripheral"
            ev.append(Event(t, "role", f"role {_addr(p[1:7])}: PC {role}"))
        elif code == 0x14 and len(p) >= 6:     # Mode Change
            handle = struct.unpack_from("<H", p, 1)[0]
            mode = {0: "active", 1: "hold", 2: "sniff"}.get(p[3], str(p[3]))
            ev.append(Event(t, "mode", f"mode {self._link(handle)}: {mode}", handle))
        elif code == 0x1B and len(p) >= 3:     # Max Slots Change
            handle = struct.unpack_from("<H", p)[0]
            ev.append(Event(t, "slots", f"max slots {self._link(handle)}: {p[2]}", handle))
        elif code == 0x1D and len(p) >= 5:     # Connection Packet Type Changed
            handle, types = struct.unpack_from("<HH", p, 1)
            ev.append(Event(t, "types", f"packet types {self._link(handle)}: {types:04x}",
                            handle))


def read(path: Path, lo: float | None = None, hi: float | None = None) -> Flow:
    """The flow in [LO, HI) (Unix times); reading stops at the first record
    past HI"""
    reader = _Reader()
    for r in records(path):
        if hi is not None and r.t >= hi:
            break
        if lo is None or r.t >= lo:
            reader.record(r)
    return reader.flow


def first_time(path: Path) -> float | None:
    """The trace's first record time, for a window given as times of day"""
    return next((r.t for r in records(path)), None)


def when(t: float) -> datetime:
    return datetime.fromtimestamp(t).astimezone()


def _common(c: Counter[int], n: int) -> str:
    return ", ".join(f"{k} x{v}" for k, v in c.most_common(n))


def kbps(flow: Flow) -> float:
    """Mean KB/s on the media links from the first media packet to the last"""
    if len(flow.packets) < 2:
        return 0.0
    span = flow.packets[-1].t - flow.packets[0].t
    return sum(s.size for s in flow.seconds()) / max(span, 1.0) / 1000


def describe(flow: Flow, rows: bool = False, show_stall: float = 0.3) -> list[str]:
    """`arb bt flow`'s lines: link events, packet shapes, throughput, stalls
    (each one longer than SHOW_STALL s), and with ROWS one line per second"""
    out = [f"{clock(when(e.t))} {e.text}" for e in flow.events]
    media = flow.handles
    frags = [f for f in flow.fragments if f.handle in media]
    out.append(f"ACL fragments {len(frags)} on the media links, sizes "
               f"{_common(Counter(f.size for f in frags), 6)}")
    if not flow.packets:
        out.append("no media packets")
        return out
    p = flow.packets
    span = p[-1].t - p[0].t
    out.append(f"media packets {len(p)} on handle {', '.join(map(str, sorted(media)))}: "
               f"{len(p) / max(span, 1.0):.1f}/s over {span:.1f} s; L2CAP sizes "
               f"{_common(Counter(x.size for x in p), 6)}; SBC frames per packet "
               f"{_common(Counter(x.frames for x in p), 4)}; RTP sequence skips {flow.skipped}")
    out.append(f"throughput {kbps(flow):.1f} KB/s")
    stalls = flow.stalls()
    out.append(f"stalls > {STALL_S * 1000:.0f} ms: {len(stalls)}, "
               f"{sum(s.seconds for s in stalls):.1f} s in all")
    out += [f"  stall {clock(when(s.t))} {s.seconds:.2f} s (seq +{s.seq_step})"
            for s in stalls if s.seconds > show_stall]
    if rows:
        out.append("second    packets frames fragments  bytes")
        out += [f"{clock(when(s.t))[:8]}  {s.packets:7d} {s.frames:6d} {s.fragments:9d} "
                f"{s.size:6d}" for s in flow.seconds()]
    return out
