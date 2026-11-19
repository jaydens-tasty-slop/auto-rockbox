"""The bench webcam, through the frames `arb webcam serve` (or any other
publisher) writes: frames during a run, and contact sheets that put the
framebuffer beside what the panel really showed. A panel fault (a shifted or
washed-out picture) never shows in the framebuffer, only on camera.

    [webcam]
    latest = "analysis/webcam/latest.png"   # the publisher's newest frame
    rotate = 0                              # degrees, to make frames upright
    every = 5                               # seconds between frames in a run
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from PIL import Image

from .errors import ArbError
from .workspace import Workspace

# The publisher writes a frame a second: an older one means it stopped (a
# reboot or power cut), and its last frame says nothing about this run.
STALE_AFTER = 10.0


class CameraError(ArbError):
    pass


def latest_path(ws: Workspace) -> Path:
    return ws.section("webcam").path("latest", "analysis/webcam/latest.png")


def frame_age(latest: Path) -> float | None:
    """Seconds since the publisher last wrote LATEST; None if it never did"""
    try:
        return time.time() - latest.stat().st_mtime
    except FileNotFoundError:
        return None


def check_fresh(ws: Workspace) -> None:
    """Before a run that wants frames: the publisher is running"""
    latest = latest_path(ws)
    age = frame_age(latest)
    if age is None or age > STALE_AFTER:
        what = "no frame" if age is None else f"its newest frame is {age:.0f} s old"
        raise CameraError(f"the webcam publisher is not running ({what}, {latest}): "
                          "start `arb webcam serve`")


class Shots:
    """Copies the newest webcam frame into DEST every few seconds while ticked;
    a stale one (the publisher stopped) is left out"""

    def __init__(self, ws: Workspace, dest: Path | None) -> None:
        self.latest = latest_path(ws)
        self.every = ws.section("webcam").number("every", 5)
        self.dest = dest
        self.t0 = time.monotonic()
        self.next = 0.0
        if dest:
            dest.mkdir(parents=True, exist_ok=True)

    def tick(self) -> None:
        if not self.dest:
            return
        now = time.monotonic() - self.t0
        if now < self.next:
            return
        age = frame_age(self.latest)
        if age is not None and age <= STALE_AFTER:
            shutil.copy(self.latest, self.dest / f"t{int(self.next):03d}.png")
        self.next += self.every


def upright(ws: Workspace, im: Image.Image) -> Image.Image:
    deg = ws.section("webcam").integer("rotate", 0)
    return im.rotate(deg, expand=True) if deg else im


def contact_sheet(ws: Workspace, frames: list[Path], out: Path, fb: Path | None = None,
                  height: int = 240) -> None:
    """FB (if any) then FRAMES in a row, each scaled to HEIGHT"""
    tiles = []
    for i, f in enumerate([fb, *frames] if fb else frames):
        try:
            im = Image.open(f).convert("RGB")
        except OSError:
            continue
        if not (fb and i == 0):
            im = upright(ws, im)
        tiles.append(im.resize((im.width * height // im.height, height)))
    if not tiles:
        return
    sheet = Image.new("RGB", (sum(t.width for t in tiles), height), "white")
    x = 0
    for t in tiles:
        sheet.paste(t, (x, 0))
        x += t.width
    sheet.save(out)
