"""Attention requests: what `arb ring` writes and what the listener accepts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from auto_rockbox import attention
from auto_rockbox.workspace import Workspace


class TestRing(unittest.TestCase):
    def test_request(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            attention.ring(root, "x" * 300)
            r = json.loads((root / "request.json").read_text())
            assert set(r) == {"id", "time", "message"} and len(r["message"]) == 240
            got = attention._request(root / "request.json")
            assert got is not None and got[0] == r["id"]
            (root / "request.json").write_text('{"id": 1, "time": 0, "message": "m"}')
            assert attention._request(root / "request.json") is None

    def test_settings(self) -> None:
        directory, sound = attention.settings(Workspace(Path("/w")))
        assert directory == Path("/w/analysis/attention")
        assert sound == Path("/usr/share/sounds/freedesktop/stereo/message.oga")
        _, sound = attention.settings(Workspace(Path("/w"), {"attention": {"sound": "a.ogg"}}))
        assert sound == Path("/w/a.ogg")

    def test_no_sound_file(self) -> None:
        assert attention.play(Path("/nonexistent/sound.oga")) is None


if __name__ == "__main__":
    unittest.main()
