"""The BTSnoop reader and media-flow analysis on a synthetic trace, and on the
stairs trace against the numbers in docs/bluetooth.md ("PC btmon at the top
of the stairs", 2026-10-29)."""

from __future__ import annotations

import struct
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from auto_rockbox.bt import flow

from .fixtures import XM3, need

T0 = 1_800_000_000.0
HANDLE = 12
ADDR = bytes([0x66, 0x55, 0x44, 0x33, 0x22, 0x11])     # 11:22:33:44:55:66


def record(t: float, op: int, data: bytes) -> bytes:
    return struct.pack(">IIIIq", len(data), len(data), op, 0,
                       round(t * 1e6) + flow.EPOCH_US) + data


def acl(boundary: int, payload: bytes, handle: int = HANDLE) -> bytes:
    return struct.pack("<HH", handle | boundary << 12, len(payload)) + payload


def media(seq: int, frames: int, size: int = 428, cid: int = 0x41) -> bytes:
    """An L2CAP frame on CID holding an RTP packet with an SBC payload"""
    rtp = bytes([0x80, 0x60]) + struct.pack(">HII", seq, seq * 640, 1)
    body = rtp + bytes([frames, 0x9C]) + bytes(size - 14)
    return struct.pack("<HH", len(body), cid) + body


def event(code: int, params: bytes) -> bytes:
    return bytes([code, len(params)]) + params


def trace(path: Path, records: list[bytes], link: int = flow.MONITOR) -> Path:
    path.write_bytes(b"btsnoop\0" + struct.pack(">II", 1, link) + b"".join(records))
    return path


def synthetic(path: Path) -> Path:
    big = media(3, 15, 965)
    return trace(path, [
        record(T0, flow.OP_COMMAND, struct.pack("<HB", 0x0405, 13) + ADDR + bytes(7)),
        record(T0 + 0.5, flow.OP_EVENT, event(0x03, bytes([0]) + struct.pack("<H", HANDLE)
                                                + ADDR + bytes([1, 0]))),
        record(T0 + 1.00, flow.OP_ACL_RX, acl(2, media(1, 5))),
        record(T0 + 1.05, flow.OP_ACL_RX, acl(2, media(2, 5))),
        # one media packet in three fragments
        record(T0 + 1.06, flow.OP_ACL_RX, acl(2, big[:339])),
        record(T0 + 1.07, flow.OP_ACL_RX, acl(1, big[339:678])),
        record(T0 + 1.08, flow.OP_ACL_RX, acl(1, big[678:])),
        # signalling on a dynamic channel, and on the L2CAP signalling one
        record(T0 + 1.09, flow.OP_ACL_RX, acl(2, struct.pack("<HH", 4, 0x40) + b"\x80\x01\0\0")),
        record(T0 + 1.10, flow.OP_ACL_RX, acl(2, struct.pack("<HH", 4, 0x01) + b"\x02\x01\0\0")),
        # seq 4 lost; a 0.45 s stall
        record(T0 + 1.55, flow.OP_ACL_RX, acl(2, media(5, 5))),
        record(T0 + 2.00, flow.OP_EVENT, event(0x05, bytes([0]) + struct.pack("<H", HANDLE)
                                                + bytes([0x08]))),
        # a new link restarts the sequence: no skip
        record(T0 + 3.00, flow.OP_ACL_RX, acl(2, media(1000, 5))),
        record(T0 + 3.01, flow.OP_ACL_RX, acl(2, media(1001, 5))),
    ])


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.path = synthetic(Path(self.dir.name) / "pc.btsnoop")

    def tearDown(self) -> None:
        self.dir.cleanup()

    def test_packets(self) -> None:
        f = flow.read(self.path)
        assert [(p.seq, p.frames, p.size) for p in f.packets] == [
            (1, 5, 428), (2, 5, 428), (3, 15, 965), (5, 5, 428), (1000, 5, 428),
            (1001, 5, 428)]
        assert f.packets[2].t == T0 + 1.08            # its last fragment
        assert f.skipped == 1
        assert f.handles == {HANDLE}

    def test_events(self) -> None:
        f = flow.read(self.path)
        assert [(e.kind, e.code) for e in f.events] == [("page", None), ("connect", 0),
                                                        ("disconnect", 0x08)]
        assert f.events[2].text == ("disconnect handle 12 (11:22:33:44:55:66): "
                                    "0x08 (connection timeout)")

    def test_seconds_and_stalls(self) -> None:
        f = flow.read(self.path)
        s = f.seconds()
        assert [(x.t - int(T0), x.packets, x.frames, x.fragments) for x in s] == [
            (1, 4, 30, 8), (3, 2, 10, 2)]
        stalls = f.stalls()
        assert [(round(x.t - T0, 2), round(x.seconds, 2), x.seq_step) for x in stalls] == [
            (1.08, 0.47, 2), (1.55, 1.45, 995)]

    def test_window(self) -> None:
        f = flow.read(self.path, T0 + 1.04, T0 + 1.5)
        assert [p.seq for p in f.packets] == [2, 3]
        assert f.events == []

    def test_describe(self) -> None:
        text = "\n".join(flow.describe(flow.read(self.path), rows=True))
        assert "SBC frames per packet 5 x5, 15 x1" in text
        assert "RTP sequence skips 1" in text
        assert "stalls > 100 ms: 2" in text
        assert "second    packets" in text

    def test_not_monitor_format(self) -> None:
        p = trace(Path(self.dir.name) / "h4.btsnoop", [], link=1002)
        with self.assertRaises(flow.FlowError):
            flow.read(p)

    def test_cut_short(self) -> None:
        data = self.path.read_bytes()
        self.path.write_bytes(data[:-10])
        assert [p.seq for p in flow.read(self.path).packets][-1] == 1000


class StairsTest(unittest.TestCase):
    """docs/bluetooth.md: ours at the stairs got 1001 stalls > 100 ms, 143 s
    in all, no RTP skips, ~930-1010-byte packets in three DH5; 41.7 KB/s at
    the desk; stock 428-byte packets of 5 frames, 69/s, ~29.8 KB/s"""

    def window(self, a: str, b: str) -> flow.Flow:
        path = need(XM3 / "stairs-pc/pc.btsnoop")
        return flow.read(path, datetime.fromisoformat(a).timestamp(),
                         datetime.fromisoformat(b).timestamp())

    def test_ours_at_the_stairs(self) -> None:
        f = self.window("2026-10-29T07:09:49-06:00", "2026-10-29T07:16:21-06:00")
        stalls = f.stalls()
        assert len(stalls) == 1001
        assert round(sum(s.seconds for s in stalls)) == 143
        assert f.skipped == 0
        sizes = [p.size for p in f.packets]
        assert min(sizes) >= 920 and max(sizes) <= 1014
        per = len([x for x in f.fragments if x.handle in f.handles]) / len(f.packets)
        assert round(per) == 3

    def test_ours_at_the_desk(self) -> None:
        f = self.window("2026-10-29T07:07:00-06:00", "2026-10-29T07:09:00-06:00")
        assert round(flow.kbps(f), 1) == 41.7

    def test_stock(self) -> None:
        f = self.window("2026-10-29T07:25:00-06:00", "2026-10-29T07:33:00-06:00")
        span = f.packets[-1].t - f.packets[0].t
        assert round(len(f.packets) / span) == 69
        assert {p.size for p in f.packets} == {428}
        assert {p.frames for p in f.packets} == {5}
        assert 29.6 <= flow.kbps(f) <= 29.9
        assert len(f.fragments) == len(f.packets)       # one baseband packet each


if __name__ == "__main__":
    unittest.main()
