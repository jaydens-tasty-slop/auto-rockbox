"""Webcam frames during a run: a stopped publisher's last frame is refused,
not passed off as the run's."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from auto_rockbox import camera
from auto_rockbox.workspace import Workspace


class TestFreshFrames(unittest.TestCase):
    def test_fresh_stale_missing(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d), {"webcam": {"latest": "latest.png", "every": 1}})
            latest = Path(d) / "latest.png"
            with self.assertRaisesRegex(camera.CameraError, "no frame"):
                camera.check_fresh(ws)
            latest.write_bytes(b"frame")
            camera.check_fresh(ws)
            old = time.time() - camera.STALE_AFTER - 60
            os.utime(latest, (old, old))
            with self.assertRaisesRegex(camera.CameraError, "s old"):
                camera.check_fresh(ws)

    def test_shots_skip_stale(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d), {"webcam": {"latest": "latest.png", "every": 1}})
            latest = Path(d) / "latest.png"
            latest.write_bytes(b"frame")
            dest = Path(d) / "shots"
            shots = camera.Shots(ws, dest)
            shots.tick()
            assert [p.name for p in dest.iterdir()] == ["t000.png"]
            old = time.time() - camera.STALE_AFTER - 60
            os.utime(latest, (old, old))
            shots.next = 0.0
            shots.tick()
            assert len(list(dest.iterdir())) == 1


if __name__ == "__main__":
    unittest.main()
