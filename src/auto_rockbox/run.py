"""One dev-loop run, the same for every target: lock the device, check the key
script, launch, watch the webcam while waiting for the image to come back,
then read the log and (if asked) the framebuffer."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import camera, fb
from .target import RunRequest, Target, Unsupported


@dataclass
class RunResult:
    returned: bool              # back in the flashing mode before the deadline
    elapsed: float
    log: str
    fb_png: Path | None = None
    sheet: Path | None = None


def run(target: Target, req: RunRequest, shots: Path | None = None) -> RunResult:
    """With SHOTS: webcam frames go there during the run, then fb.png and a
    contact sheet (sheet.png) of both"""
    target.check_keys(req.keys)
    with target.lock():
        target.before_run(req)
        target.launch(req)
        t0 = time.monotonic()
        cam = camera.Shots(target.ws, shots)
        returned = target.wait_return(t0 + req.seconds + target.return_margin, cam.tick)
        elapsed = time.monotonic() - t0
        log = target.read_log() if returned else ""
        png = sheet = None
        if returned and shots and target.has("framebuffer"):
            try:
                png = shots / "fb.png"
                fb.scaled(target.read_framebuffer(req.image)).save(png)
            except (Unsupported, OSError, ValueError) as e:
                log += f"\n[arb] no framebuffer: {e}\n"
                png = None
        target.after_run(req, returned)
    if shots:
        sheet = shots / "sheet.png"
        camera.contact_sheet(target.ws, sorted(shots.glob("t*.png")), sheet, png)
    return RunResult(returned, elapsed, log, png, sheet)
