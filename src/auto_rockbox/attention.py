"""Calling the person at the bench. An agent in a sandbox has no sound and
no desktop, but can write files: it leaves a request, and a listener in the
person's desktop session plays a sound and prints the message. The request
holds text only: it cannot choose a command or a sound file.

    [attention]
    dir = "analysis/attention"      # request.json and the listener's status.json
    sound = "/usr/share/sounds/freedesktop/stereo/message.oga"
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .publish import atomic_write
from .workspace import Workspace

MAX_MESSAGE = 240
FRESH_S = 120               # an older request is a leftover, not a call
POLL_S = 0.2
PLAYERS = ("paplay", "pw-play")


def settings(ws: Workspace) -> tuple[Path, Path]:
    """The request directory and the sound"""
    c = ws.section("attention")
    return (c.path("dir", "analysis/attention"),
            c.path("sound", "/usr/share/sounds/freedesktop/stereo/message.oga"))


def _save(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, (json.dumps(value) + "\n").encode())


def ring(directory: Path, message: str) -> None:
    _save(directory / "request.json",
          {"id": uuid.uuid4().hex, "time": time.time(), "message": message[:MAX_MESSAGE]})


def play(sound: Path) -> str | None:
    """Plays SOUND; the player that worked, or None"""
    if not sound.is_file():
        return None
    for name in PLAYERS:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            if subprocess.run([exe, str(sound)], capture_output=True, timeout=5,
                              check=False).returncode == 0:
                return name
        except (OSError, subprocess.TimeoutExpired):
            continue
    return None


def _request(path: Path) -> tuple[str, float, str] | None:
    try:
        r = json.loads(path.read_text())
        ident, when, message = r["id"], r["time"], r["message"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if isinstance(ident, str) and isinstance(when, int | float) and isinstance(message, str):
        return ident, float(when), message[:MAX_MESSAGE]
    return None


def listen(directory: Path, sound: Path, echo: Callable[[str], object] = print) -> None:
    """Plays SOUND and prints the message for each new request, until
    interrupted. A test sound first proves the audio works; a request that
    was already there is not announced."""
    request, status = directory / "request.json", directory / "status.json"
    old = _request(request)
    seen = old[0] if old else None
    backend = play(sound)
    if not backend:
        _save(status, {"ready": False, "time": time.time(), "error": f"cannot play {sound}"})
        raise RuntimeError(f"cannot play {sound} with {' or '.join(PLAYERS)}: run this in "
                           "the desktop session")
    _save(status, {"ready": True, "time": time.time(), "backend": backend, "last_id": seen})
    echo("sound test played; listening (Ctrl-C stops)")
    try:
        while True:
            r = _request(request)
            if r and r[0] != seen:
                seen, when, message = r
                if 0 <= time.time() - when <= FRESH_S:
                    backend = play(sound)
                    echo(f"{time.strftime('%H:%M:%S')} {message}")
                    _save(status, {"ready": bool(backend), "time": time.time(),
                                   "backend": backend, "last_id": seen, "message": message})
            time.sleep(POLL_S)
    finally:
        _save(status, {"ready": False, "time": time.time(), "stopped": True})
