"""The strict sink's media checks on made-up RTP streams, and the verdict
that judge() gives a run."""

from __future__ import annotations

import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from auto_rockbox.btpeer import media, sbc
from auto_rockbox.btpeer.analysis import WavAnalysis

from .fixtures import JOINT, frame, packet, rtp, stream

MTU = 672
CONFIG = sbc.Config(44100, 3, 16, 8, 0, 2, 53)


@contextmanager
def recorder(config: sbc.Config | None = CONFIG) -> Generator[media.Recorder]:
    with tempfile.TemporaryDirectory() as tmp:
        rec = media.Recorder(Path(tmp) / "stream.sbc")
        if config is not None:
            rec.new_stream(config)
        try:
            yield rec
        finally:
            rec.stop()


def feed(rec: media.Recorder, pdus: list[bytes], state: str = "STREAMING") -> None:
    for pdu in pdus:
        rec.on_media(pdu, MTU, state)


def test_clean_stream() -> None:
    with recorder() as rec:
        feed(rec, stream(20))
        assert rec.problems == {}
        assert rec.samples == 20 * 5 * 128
        assert abs(rec.audio_seconds - 12800 / 44100) < 1e-9
        assert rec.frames_per_packet == {5: 20}
        assert rec.bitpools == {35: 100}
        rec.stop()
        assert rec.path.read_bytes() == frame(JOINT) * 100


def test_lost_packet_and_millisecond_timestamps() -> None:
    with recorder() as rec:
        pdus = stream(10)
        feed(rec, pdus[:4] + pdus[5:])          # one lost: sequence and timestamp jump
        assert rec.problems == {"seq": 1, "timestamp": 1}
    with recorder() as rec:                     # stamped in ms, not in samples
        feed(rec, [packet(i, i * 15, 5) for i in range(5)])
        assert rec.problems == {"timestamp": 4}
    with recorder() as rec:
        pdus = stream(3)
        feed(rec, [pdus[0], pdus[2], pdus[1]])
        assert rec.problems["seq"] == 2         # skipped one, then went backwards


def test_fragmented_frame() -> None:
    whole = frame(JOINT)
    half = len(whole) // 2
    with recorder() as rec:
        feed(rec, [rtp(0, 0, bytes([0xC1]) + whole[:half]),     # first of two fragments
                   rtp(1, 0, bytes([0xA0]) + whole[half:]),     # the last
                   packet(2, 128, 5)])
        assert rec.counts["fragmented"] == 2
        assert rec.problems == {}                   # the fragments share a timestamp
        assert rec.samples == 6 * 128
    with recorder() as rec:
        feed(rec, [rtp(0, 0, bytes([0xA0]) + whole[half:])])
        assert rec.problems == {"sbc": 1}           # a last fragment without a first


def test_frame_and_header_problems() -> None:
    bad_crc = bytearray(frame(JOINT))
    bad_crc[6] ^= 1
    stereo = sbc.Frame(44100, 16, 2, 0, 8, 35)
    with recorder() as rec:
        feed(rec, [rtp(0, 0, bytes([1]) + bytes(bad_crc)),
                   packet(1, 128, 1, stereo),               # not the configuration
                   rtp(2, 256, bytes([3]) + frame(JOINT)),  # says 3 frames, has 1
                   rtp(3, 384, bytes([1]) + frame(JOINT)[:40]),    # truncated
                   rtp(4, 384, b"")])
        # the truncated frame: also one frame short of its payload header's count
        assert rec.problems == {"crc": 1, "config": 1, "sbc": 3, "rtp": 1}


def test_mtu_state_and_ssrc() -> None:
    with recorder() as rec:
        rec.on_media(packet(0, 0, 9), 500, "STREAMING")     # 9 frames: 760 bytes
        rec.on_media(packet(1, 9 * 128, 1), MTU, "OPEN")
        rec.on_media(rtp(2, 10 * 128, bytes([1]) + frame(JOINT), ssrc=7), MTU, "STREAMING")
        assert rec.problems == {"mtu": 1, "state": 1, "rtp": 1}


def test_reconfiguration_and_restart() -> None:
    with recorder() as rec:
        feed(rec, stream(3))
        rec.restart()                               # a new segment: no sequence check
        feed(rec, [packet(100, 0, 5)])
        assert rec.problems == {}
        rec.new_stream(sbc.Config(48000, 3, 16, 8, 0, 2, 53))
        assert rec.problems == {"config": 1}
        assert {p.segment for p in rec.packets} == {1, 2}


CLEAN = WavAnalysis(44100, 2, 10.0, [1000.0, 1000.0], [], [])


def _clean(*, received: bool = True, problems: dict[str, int] | None = None,
           audio_seconds: float = 10.0, timing: media.Timing | None = None,
           wav: WavAnalysis | None = CLEAN, decoded: bool = True, violations: str = "",
           hf_status: str | None = None) -> list[str]:
    """judge() on a clean 10 s run of a 1 kHz tone, but for the changes"""
    return media.judge(received=received, problems=problems or {},
                       audio_seconds=audio_seconds, timing=timing or media.Timing([], [1.0]),
                       wav=wav, decoded=decoded, expect_freq=1000.0, seconds=10.0,
                       violations=violations, hf_status=hf_status)


def test_judge() -> None:
    assert _clean() == []
    assert _clean(received=False, wav=None, audio_seconds=0.0) == ["no media", "too short"]
    assert _clean(decoded=False, wav=None) == ["ffmpeg decode failed", "tone"]
    assert _clean(wav=WavAnalysis(44100, 2, 10.0, [1000.0, 1020.0], [], [])) == []
    assert _clean(wav=WavAnalysis(44100, 2, 10.0, [1000.0, None], [], [])) == ["tone"]
    everything = _clean(
        wav=WavAnalysis(44100, 2, 9.0, [900.0, 1000.0], [(3.0, 20.0)], []),
        timing=media.Timing([(2.0, 150.0)], [0.95]),
        problems={"timestamp": 2, "seq": 1}, hf_status="down", violations="acl 2",
        audio_seconds=9.0)
    assert everything == [
        "tone", "audio gaps", "stalls",
        "not real time (audio/wall rate 0.950; a headset buffer would under/overrun)",
        "seq", "timestamp", "HFP: down", "HCI/L2CAP violations acl 2", "too short"]
    assert _clean(hf_status="up (battery 80 %)") == []
