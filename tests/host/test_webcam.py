"""Webcam publisher: PNG stream cutting, the frame drain, focus parsing and
settings, without a camera."""

from __future__ import annotations

import io
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from auto_rockbox import webcam
from auto_rockbox.workspace import ConfigError, Workspace

LISTING = """
User Controls

                     brightness 0x00980900 (int)    : min=0 max=255 step=1 default=128 value=128
Camera Controls

                 focus_absolute 0x009a090a (int)    : min=0 max=250 step=5 default=0 value=75
     focus_automatic_continuous 0x009a090c (bool)   : default=1 value=0
"""


def png(size: tuple[int, int], colour: str) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


class TestFrames(unittest.TestCase):
    def test_read_png(self) -> None:
        a, b = png((32, 18), "red"), png((18, 32), "blue")
        stream = io.BytesIO(a + b + a[:20])
        assert webcam.read_png(stream) == a
        got = webcam.read_png(stream)
        assert got == b and webcam.png_size(got) == (18, 32)
        with self.assertRaises(EOFError):
            webcam.read_png(stream)
        with self.assertRaises(ValueError):
            webcam.read_png(io.BytesIO(b"\xff\xd8 not a png at all"))

    @unittest.skipUnless(shutil.which("sh"), "no sh")
    def test_stream_keeps_the_newest(self) -> None:
        """Three frames, then a stream that stays open: a late reader gets the last"""
        frames = [png((8, 8), c) for c in ("red", "green", "blue")]
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "frames.bin"
            src.write_bytes(b"".join(frames))
            with webcam.Stream(["sh", "-c", f"cat '{src}' && exec sleep 10"]) as stream:
                time.sleep(0.5)
                assert stream.next(timeout=5).png == frames[-1]
                with self.assertRaises(TimeoutError):
                    stream.next(timeout=0.2)

    def test_rotation(self) -> None:
        assert webcam.rotation_filters(90) == ["transpose=2"]
        assert webcam.rotation_filters(-90) == ["transpose=1"]
        with self.assertRaises(ConfigError):
            webcam.rotation_filters(45)


class TestFocus(unittest.TestCase):
    def test_parse(self) -> None:
        r = webcam.parse_controls(LISTING)
        assert r == webcam.FocusRange("focus_automatic_continuous", 0, 250, 5)
        assert r.accepts(75) == {75} and r.accepts(77) == {75, 80}
        state = webcam.parse_readback("focus_automatic_continuous: 0\nfocus_absolute: 75\n",
                                      r.auto)
        assert state == webcam.FocusState(False, 75)
        assert state.as_json() == {"autofocus": 0, "focus_absolute": 75}
        with self.assertRaises(webcam.FocusError):
            webcam.parse_controls("brightness 0x00980900 (int) : min=0 max=255 step=1")

    def test_settings(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d), {"webcam": {"focus": 75, "capture_rotate": 270}})
            s = webcam.Settings.of(ws)
            assert s.focus == 75 and s.latest == Path(d) / "analysis/webcam/latest.png"
            cmd = webcam.ffmpeg_command(s)
            assert cmd[cmd.index("-vf") + 1] == "fps=1,transpose=1"
            assert webcam.Settings.of(Workspace(Path(d))).focus == webcam.DEFAULT_FOCUS
            assert webcam.Settings.of(Workspace(Path(d), {"webcam": {"focus": "auto"}})).focus \
                is None
            with self.assertRaises(ConfigError):
                webcam.Settings.of(Workspace(Path(d), {"webcam": {"size": "big"}}))


if __name__ == "__main__":
    unittest.main()
