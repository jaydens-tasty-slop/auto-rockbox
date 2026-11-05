"""The virtual sink's media checks: every RTP packet the stack sends is
checked as a strict headset would (RTP header, sequence and timestamps, SBC
frames, their CRC and configuration, the L2CAP MTU, the AVDTP state), the
SBC frames are kept in a file, and the arrivals are timed (stalls, jitter
against the audio clock, real-time rate). judge() turns a run into PASS or
FAIL."""

from __future__ import annotations

import asyncio
import struct
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .analysis import WavAnalysis
from .logs import log
from .sbc import Config, crc_ok, frame_header

STALL_MS = 100              # a longer wait between packets of one stream segment
RATE_SPAN_S = 5.0           # a segment this long has its audio/wall rate judged
RATE_OK = (0.99, 1.02)      # outside it a headset's buffer would under- or overrun
TONE_TOLERANCE = 0.02
# Media problems that fail a run (fragmented SBC frames are only counted)
FAILING = ("seq", "sbc", "crc", "config", "rtp", "mtu", "state", "timestamp")


@dataclass(frozen=True)
class Packet:
    arrival: float          # time.monotonic()
    segment: int            # the stream segment (a new one after each START)
    seq: int
    ts: int
    size: int
    frames: int
    samples: int


class Recorder:
    """The stream as the sink receives it: checks, SBC file, arrivals"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.file = path.open("wb")
        self.config: Config | None = None
        self.rate: int | None = None
        self.packets: list[Packet] = []
        self.samples = 0
        self.t_first: float | None = None
        self.first_packet = asyncio.Event()
        self.segment = 0
        self.prev: tuple[int, int, int, int] | None = None   # seq, ts, samples, ssrc
        self.frag: bytearray | None = None
        self.stopped = False
        self.counts: Counter[str] = Counter()
        self.sizes: Counter[int] = Counter()
        self.frames_per_packet: Counter[int] = Counter()
        self.bitpools: Counter[int] = Counter()
        self.last_progress = 0.0

    def problem(self, kind: str, msg: str) -> None:
        self.counts[kind] += 1
        if self.counts[kind] <= 5:
            log.warning("media: %s", msg)
        elif self.counts[kind] == 6:
            log.warning('media: further "%s" problems are counted, not logged', kind)

    @property
    def problems(self) -> dict[str, int]:
        return {k: v for k, v in self.counts.items() if k != "fragmented"}

    def new_stream(self, config: Config) -> None:
        """A (re)configuration"""
        self.segment += 1
        self.prev = None
        self.frag = None
        if self.config is not None and self.config != config:
            self.problem("config", "stream reconfigured: the recording mixes "
                                   "configurations, analysis assumes one")
        self.config = config

    def restart(self) -> None:
        """A START after a suspend: a new segment, so the suspended time is
        not counted as a stall"""
        self.segment += 1
        self.prev = None
        self.frag = None

    def on_media(self, pdu: bytes, mtu: int, state: str) -> None:
        """One media packet; STATE: the stream's AVDTP state name"""
        if self.stopped:
            return
        now = time.monotonic()
        if len(pdu) > mtu:
            self.problem("mtu", f"{len(pdu)}-byte media packet > sink L2CAP MTU {mtu}")
        if state != "STREAMING":
            self.problem("state", f"media packet in AVDTP state {state} (expected STREAMING)")
        if len(pdu) < 13:
            self.problem("rtp", f"{len(pdu)}-byte media packet (no RTP + SBC header)")
            return
        version, cc = pdu[0] >> 6, pdu[0] & 0x0F
        seq, ts, ssrc = struct.unpack_from(">HII", pdu, 2)
        off = 12 + 4 * cc
        if version != 2:
            self.problem("rtp", f"RTP version {version}")
        if pdu[0] & 0x10:
            self.problem("rtp", "RTP header extension present")
            off += 4 + 4 * struct.unpack_from(">H", pdu, off + 2)[0]
        end = len(pdu) - (pdu[-1] if pdu[0] & 0x20 else 0)
        payload = pdu[off:end]
        if not payload:
            self.problem("rtp", "empty RTP payload")
            return
        hdr = payload[0]
        frames = None
        if hdr & 0x80:                          # a fragmented SBC frame
            if not self.counts["fragmented"]:
                log.info("media: fragmented SBC frames in use")
            self.counts["fragmented"] += 1
            if hdr & 0x40:
                self.frag = bytearray(payload[1:])
            elif self.frag is None:
                self.problem("sbc", f"seq {seq}: SBC fragment without a start fragment")
            else:
                self.frag += payload[1:]
            if hdr & 0x20 and self.frag is not None:
                frames, self.frag = bytes(self.frag), None
        else:
            if self.frag is not None:
                self.problem("sbc", f"seq {seq}: unfinished fragmented SBC frame dropped")
                self.frag = None
            frames = payload[1:]
        nframes = samples = 0
        if frames is not None:
            nframes, samples = self.take_frames(frames, seq)
            if not hdr & 0x80 and nframes != hdr & 0x0F:
                self.problem("sbc", f"seq {seq}: payload header says {hdr & 0x0F} frames, "
                                    f"found {nframes}")
        if self.prev is not None:
            pseq, pts, psamples, pssrc = self.prev
            dseq = (seq - pseq) & 0xFFFF
            if dseq != 1:
                what = f"{dseq - 1} lost" if dseq < 0x8000 else "went backwards"
                self.problem("seq", f"RTP sequence {pseq} -> {seq} ({what})")
            dts = (ts - pts) & 0xFFFFFFFF
            if psamples and dts != psamples:
                self.problem("timestamp", f"RTP timestamp +{dts} after a packet of "
                                          f"{psamples} samples (expected +{psamples})")
            if ssrc != pssrc:
                self.problem("rtp", f"SSRC changed 0x{pssrc:08x} -> 0x{ssrc:08x}")
        self.prev = (seq, ts, samples, ssrc)
        if self.t_first is None:
            self.t_first = self.last_progress = now
            self.first_packet.set()
            log.info("media: first packet: %d bytes, %d SBC frames, seq %d, ts %d, PT %d, "
                     "SSRC 0x%08x", len(pdu), nframes, seq, ts, pdu[1] & 0x7F, ssrc)
        self.packets.append(Packet(now, self.segment, seq, ts, len(pdu), nframes, samples))
        self.sizes[len(pdu)] += 1
        self.frames_per_packet[nframes] += 1
        if now - self.last_progress >= 5:
            self.last_progress = now
            n = len(self.packets)
            log.info("media: %.1f s audio in %d packets (%.1f packets/s), last %d bytes / "
                     "%d frames", self.audio_seconds, n, n / max(now - self.t_first, 1e-6),
                     len(pdu), nframes)

    def take_frames(self, data: bytes, seq: int) -> tuple[int, int]:
        """Checks and keeps the SBC frames in DATA: (frames, samples)"""
        off = nframes = samples = 0
        while off < len(data):
            f = frame_header(data, off)
            if isinstance(f, str):
                self.problem("sbc", f"seq {seq}: {f}")
                break
            if off + f.length > len(data):
                self.problem("sbc", f"seq {seq}: truncated SBC frame "
                                    f"({len(data) - off} of {f.length} bytes)")
                break
            frame = data[off:off + f.length]
            if not crc_ok(frame, f):
                self.problem("crc", f"seq {seq}: SBC frame CRC mismatch")
            if self.config is not None and (bad := self.config.mismatches(f)):
                self.problem("config", f"seq {seq}: SBC frame does not match the "
                                       f"configuration: {', '.join(bad)}")
            if self.rate is None:
                self.rate = f.rate
            elif f.rate != self.rate:
                self.problem("config", f"seq {seq}: sampling rate changed to {f.rate}")
            self.bitpools[f.bitpool] += 1
            self.file.write(frame)
            nframes += 1
            samples += f.samples
            off += f.length
        self.samples += samples
        return nframes, samples

    @property
    def audio_seconds(self) -> float:
        return self.samples / self.rate if self.rate else 0.0

    async def wait_done(self, seconds: float) -> None:
        """Until SECONDS of audio arrived (counted from the first packet's
        arrival, then up to 2 s more for the last packets)"""
        await self.first_packet.wait()
        assert self.t_first is not None
        await asyncio.sleep(max(0.0, self.t_first + seconds - time.monotonic()))
        for _ in range(20):
            if self.audio_seconds >= seconds:
                break
            await asyncio.sleep(0.1)

    def stop(self) -> None:
        self.stopped = True
        self.file.close()

    def summary(self, out: Callable[[str], object]) -> Timing:
        """The packets' report lines (to OUT) and their timing"""
        timing = Timing([], [])
        if not self.packets:
            out("media: no packets received")
            return timing
        p = self.packets
        arrivals = np.array([x.arrival for x in p])
        out(f"media: {len(p)} packets in {self.segment} stream(s), "
            f"{self.audio_seconds:.2f} s of audio over {arrivals[-1] - arrivals[0]:.2f} s")
        out("  packet sizes: " + ", ".join(f"{s} B x{n}" for s, n in self.sizes.most_common(6)))
        out("  SBC frames per packet: "
            + ", ".join(f"{f} x{n}" for f, n in sorted(self.frames_per_packet.items()))
            + "; bitpools used: "
            + ", ".join(f"{b} x{n}" for b, n in sorted(self.bitpools.items())))
        if self.counts["fragmented"]:
            out(f"  fragmented-frame packets: {self.counts['fragmented']}")
        out("  problems: "
            + (", ".join(f"{k} {v}" for k, v in self.problems.items()) or "none"))
        gaps_ms = np.diff(arrivals) * 1000
        if len(gaps_ms):
            out(f"  inter-arrival ms: median {np.median(gaps_ms):.1f}, p99 "
                f"{np.percentile(gaps_ms, 99):.1f}, max {gaps_ms.max():.1f}")
        first = self.t_first or 0.0
        timing.stalls += [(float(arrivals[i] - first), float(g)) for i, g in enumerate(gaps_ms)
                          if g > STALL_MS and p[i].segment == p[i + 1].segment]
        for t, g in timing.stalls[:10]:
            out(f"  STALL: {g:.0f} ms without media after +{t:.2f} s")
        for index in sorted({x.segment for x in p}):
            seg = [x for x in p if x.segment == index]
            if len(seg) < 3 or not self.rate:
                continue
            arr = np.array([x.arrival for x in seg])
            pos = np.concatenate(([0], np.cumsum([x.samples for x in seg])[:-1])) / self.rate
            late = (arr - arr[0]) - pos
            late -= late.min()
            span = float(arr[-1] - arr[0])
            if span >= RATE_SPAN_S:
                timing.rates.append(float(pos[-1]) / span)
            out(f"  stream {index}: arrival jitter (vs audio clock) p50 "
                f"{np.median(late) * 1000:.1f} ms, p99 {np.percentile(late, 99) * 1000:.1f}"
                f" ms, max {late.max() * 1000:.1f} ms; audio/wall rate "
                f"{pos[-1] / span if span else 0:.4f}")
        return timing


@dataclass
class Timing:
    stalls: list[tuple[float, float]]       # (after +s, ms) within a segment
    rates: list[float]                      # audio/wall per segment of RATE_SPAN_S or more


def judge(*, received: bool, problems: dict[str, int], audio_seconds: float,
          timing: Timing, wav: WavAnalysis | None, decoded: bool, expect_freq: float,
          seconds: float, violations: str, hf_status: str | None) -> list[str]:
    """Why the run fails; empty: it passes. WAV: the decoded audio (None if
    nothing was received or decoding failed); HF_STATUS: the HFP link's state
    when the run had one"""
    reasons = []
    if not received:
        reasons.append("no media")
    elif not decoded:
        reasons.append("ffmpeg decode failed")
    medians: Sequence[float | None] = wav.medians if wav else []
    tol = expect_freq * TONE_TOLERANCE
    if received and (not medians or any(m is None or abs(m - expect_freq) > tol
                                        for m in medians)):
        reasons.append("tone")
    if wav and wav.gaps:
        reasons.append("audio gaps")
    if timing.stalls:
        reasons.append("stalls")
    if any(not RATE_OK[0] <= r <= RATE_OK[1] for r in timing.rates):
        reasons.append("not real time (audio/wall rate "
                       + "/".join(f"{r:.3f}" for r in timing.rates)
                       + "; a headset buffer would under/overrun)")
    reasons += [kind for kind in FAILING if problems.get(kind)]
    if hf_status is not None and "up" not in hf_status:
        reasons.append("HFP: " + hf_status)
    if violations:
        reasons.append("HCI/L2CAP violations " + violations)
    if audio_seconds < seconds:
        reasons.append("too short")
    return reasons
