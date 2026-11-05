"""One dev-loop run, the same for every target: lock the device, check the key
script, launch, watch the webcam while waiting for the image to come back,
then read the log and (if asked) the framebuffer."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import camera, fb
from .errors import UsageError
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
        elf = req.image if req.image.suffix == ".elf" else None
        log = target.read_log(elf) if returned else ""
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


def parse_settings(pairs: list[str]) -> dict[str, str]:
    """--set KEY=VALUE arguments as a dict"""
    out = {}
    for p in pairs:
        k, sep, v = p.partition("=")
        if not sep:
            raise UsageError(f"--set {p}: KEY=VALUE needed")
        out[k] = v
    return out


def report(target: Target, req: RunRequest, r: RunResult) -> int:
    """Prints a run's outcome and log; the exit status (3: no return in time)"""
    if not r.returned:
        print(f"no return within {req.seconds + target.return_margin:.0f} s", file=sys.stderr)
        return 3
    print(f"back after {r.elapsed:.1f} s")
    print(r.log, end="" if r.log.endswith("\n") else "\n")
    if r.sheet and r.sheet.exists():
        print(f"contact sheet {r.sheet}")
    return 0
