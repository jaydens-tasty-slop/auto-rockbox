"""The bench webcam publisher: a frame a second from a V4L2 camera, as a PNG
that agents and run contact sheets (auto_rockbox.camera) look at.

`arb webcam serve` runs ffmpeg on the camera and replaces [webcam] latest
with each new frame, with a JSON beside it (capture time, frame number,
focus); with a history it also keeps the last frames in a ring of slots
beside it (history/NNN/latest.png and latest.json).

Focus is manual and fixed: autofocus hunts on a small bright panel. The
camera's readback of focus_absolute did not prove where the lens was:
frames came out sharp only after moving it to its minimum and back, so
setting a focus goes that way. `arb webcam focus --sweep` saves frames at
several settings from the running publisher, to choose one by eye.

    [webcam]
    latest = "analysis/webcam/latest.png"   # where frames go (camera.py reads it)
    device = "/dev/video0"
    size = "1280x720"
    focus = 123             # focus_absolute, or "auto"
    capture_rotate = 90     # degrees counter-clockwise the publisher turns frames
    history = 0             # frames kept in history/ (one a second)
    rotate = 0              # (camera.py) degrees that make published frames upright
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import queue
import re
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO

from .errors import ArbError
from .publish import Ring, atomic_write, exclusive
from .workspace import ConfigError, Workspace

DEFAULT_FOCUS = 123
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_FRAME = 32 << 20            # a corrupt stream must not make us buffer without end
FRAME_TIMEOUT_S = 15.0


@dataclass(frozen=True)
class Settings:
    latest: Path
    device: str
    size: str                   # WIDTHxHEIGHT the camera delivers
    focus: int | None           # None: autofocus
    capture_rotate: int
    history: int

    @classmethod
    def of(cls, ws: Workspace) -> Settings:
        c = ws.section("webcam")
        focus = c.get("focus", DEFAULT_FOCUS)
        if focus != "auto" and not isinstance(focus, int):
            raise ConfigError('[webcam] focus: an integer or "auto" is needed')
        size = c.string("size", "1280x720")
        if not re.fullmatch(r"\d+x\d+", size):
            raise ConfigError("[webcam] size: WIDTHxHEIGHT is needed")
        rotate = c.integer("capture_rotate", 90)
        rotation_filters(rotate)
        return cls(c.path("latest", "analysis/webcam/latest.png"),
                   c.string("device", "/dev/video0"), size,
                   None if focus == "auto" else focus, rotate, c.integer("history", 0))


def parse_focus(text: str) -> int | None:
    """A focus on the command line: a number, or "auto" (None)"""
    return None if text == "auto" else int(text)


# ---------------------------------------------------------------- frames

def _read_exact(stream: IO[bytes], size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError("the camera stream ended inside a frame")
        data += chunk
    return bytes(data)


def read_png(stream: IO[bytes]) -> bytes:
    """The next PNG of a stream of them (ffmpeg's image2pipe), cut at its IEND chunk"""
    frame = bytearray(_read_exact(stream, 8))
    if frame != PNG_SIGNATURE:
        raise ValueError("the camera stream is not PNG")
    while True:
        header = _read_exact(stream, 8)
        size, kind = struct.unpack(">I4s", header)
        if len(frame) == 8 and (kind != b"IHDR" or size != 13):
            raise ValueError("a PNG without its IHDR")
        if len(frame) + 12 + size > MAX_FRAME:
            raise ValueError("a frame over 32 MiB: the stream is corrupt")
        frame += header + _read_exact(stream, size + 4)
        if kind == b"IEND":
            return bytes(frame)


def png_size(png: bytes) -> tuple[int, int]:
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def rotation_filters(degrees: int) -> list[str]:
    """ffmpeg filters that turn frames DEGREES counter-clockwise"""
    turns = {0: [], 90: ["transpose=2"], 180: ["hflip", "vflip"], 270: ["transpose=1"]}
    if degrees % 360 not in turns:
        raise ConfigError("[webcam] capture_rotate: 0, 90, 180 or 270 is needed")
    return turns[degrees % 360]


def ffmpeg_command(s: Settings) -> list[str]:
    """MJPEG from the camera at 15 fps (cheap over USB), one PNG a second out"""
    vf = ",".join(["fps=1", *rotation_filters(s.capture_rotate)])
    return ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning",
            "-f", "video4linux2", "-input_format", "mjpeg", "-video_size", s.size,
            "-framerate", "15", "-i", s.device, "-an", "-vf", vf,
            "-c:v", "png", "-pix_fmt", "rgb24", "-f", "image2pipe", "pipe:1"]


@dataclass(frozen=True)
class Frame:
    received: float             # time.monotonic()
    captured: datetime
    png: bytes


class Stream:
    """A frame source (ffmpeg on the camera) drained by a thread that keeps
    only the newest frame: a reader that falls behind gets the current
    picture, not a backlog, and ffmpeg never blocks on a full pipe."""

    def __init__(self, cmd: list[str]) -> None:
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     bufsize=0)
        self.frames: queue.Queue[Frame | Exception] = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._drain, daemon=True)
        self.thread.start()

    def _offer(self, item: Frame | Exception) -> None:
        while True:
            try:
                self.frames.put_nowait(item)
                return
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self.frames.get_nowait()

    def _drain(self) -> None:
        out = self.proc.stdout
        if out is None:
            self._offer(OSError("no frame pipe"))
            return
        try:
            while True:
                png = read_png(out)
                self._offer(Frame(time.monotonic(), datetime.now(UTC), png))
        except (OSError, EOFError, ValueError) as e:
            self._offer(e)

    def next(self, timeout: float = FRAME_TIMEOUT_S) -> Frame:
        try:
            item = self.frames.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no camera frame within {timeout:.0f} s") from None
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.thread.join(timeout=3)
        if self.proc.stdout:
            self.proc.stdout.close()

    def __enter__(self) -> Stream:
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


# ---------------------------------------------------------------- focus

@dataclass(frozen=True)
class FocusRange:
    auto: str                   # the autofocus control's name, which cameras differ in
    minimum: int
    maximum: int
    step: int

    def accepts(self, requested: int) -> set[int]:
        """Readbacks that mean REQUESTED took: the camera rounds to its step"""
        lower = self.minimum + (requested - self.minimum) // self.step * self.step
        if requested == lower:
            return {requested}
        return {lower, min(lower + self.step, self.maximum)}


@dataclass(frozen=True)
class FocusState:
    autofocus: bool
    absolute: int

    def as_json(self) -> dict[str, int]:
        return {"autofocus": int(self.autofocus), "focus_absolute": self.absolute}

    def __str__(self) -> str:
        return f"autofocus (at {self.absolute})" if self.autofocus else f"manual {self.absolute}"


class FocusError(ArbError):
    pass


def parse_controls(listing: str) -> FocusRange:
    """The focus controls in `v4l2-ctl --list-ctrls` output"""
    names = set(re.findall(r"^\s*([a-z][a-z0-9_]+)\s+0x", listing, re.M))
    auto = next((n for n in ("focus_automatic_continuous", "focus_auto") if n in names), None)
    line = next((ln for ln in listing.splitlines()
                 if re.match(r"^\s*focus_absolute\s+0x", ln)), None)
    if auto is None or line is None:
        raise FocusError("the camera has no autofocus and focus_absolute controls:\n" + listing)
    fields = dict(re.findall(r"\b(min|max|step)=(-?\d+)", line))
    if set(fields) != {"min", "max", "step"} or int(fields["step"]) < 1:
        raise FocusError("no focus range in: " + line)
    return FocusRange(auto, int(fields["min"]), int(fields["max"]), int(fields["step"]))


def parse_readback(output: str, auto: str) -> FocusState:
    values = dict(re.findall(r"^([a-z_]+):\s*(-?\d+)", output, re.M))
    if auto not in values or "focus_absolute" not in values:
        raise FocusError("incomplete focus readback: " + output)
    return FocusState(bool(int(values[auto])), int(values["focus_absolute"]))


class Focus:
    """The camera's focus through v4l2-ctl, which works while another process streams"""

    def __init__(self, device: str) -> None:
        self.device = device
        self.range = parse_controls(self._ctl("--list-ctrls"))

    def _ctl(self, option: str) -> str:
        try:
            r = subprocess.run(["v4l2-ctl", "-d", self.device, option], stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=5, check=False)
        except FileNotFoundError:
            raise FocusError("v4l2-ctl is missing (v4l-utils)") from None
        if r.returncode:
            raise FocusError(r.stderr.strip() or r.stdout.strip() or "v4l2-ctl failed")
        return r.stdout

    def read(self) -> FocusState:
        return parse_readback(self._ctl(f"--get-ctrl={self.range.auto},focus_absolute"),
                              self.range.auto)

    def apply(self, requested: int | None, settle: float = 1.0) -> FocusState:
        """REQUESTED (None: autofocus), checked by readback after SETTLE s"""
        r = self.range
        if requested is not None and not r.minimum <= requested <= r.maximum:
            raise FocusError(f"focus must be within {r.minimum}-{r.maximum}")
        self._ctl(f"--set-ctrl={r.auto}={int(requested is None)}")
        if requested is not None:
            self._ctl(f"--set-ctrl=focus_absolute={requested}")
        time.sleep(settle)
        state = self.read()
        if state.autofocus != (requested is None):
            raise FocusError(f"autofocus did not switch: {state}")
        if requested is not None and state.absolute not in r.accepts(requested):
            raise FocusError(f"asked for {requested}, the camera reports {state} "
                             f"(range {r.minimum}-{r.maximum}, step {r.step})")
        return state

    def set(self, requested: int | None) -> FocusState:
        """REQUESTED, reached from the minimum (see the module docstring)"""
        if requested is not None and requested != self.range.minimum:
            self.apply(self.range.minimum, settle=2)
        return self.apply(requested, settle=2)


# ---------------------------------------------------------------- publishing

def serve(s: Settings, echo: Callable[[str], object] = print) -> None:
    """Publishes a frame a second until interrupted"""
    directory = s.latest.parent
    meta_path = s.latest.with_suffix(".json")
    with exclusive(directory), Stream(ffmpeg_command(s)) as stream:
        stream.next()                       # a frame: the camera really streams
        focus = Focus(s.device)
        r = focus.range
        echo(f"focus {focus.read()}; range {r.minimum}-{r.maximum}, step {r.step}")
        state = focus.set(s.focus)
        echo(f"focus asked {'auto' if s.focus is None else s.focus}, camera reports {state}")
        ready = time.monotonic()            # frames from before the lens settled are stale
        ring = Ring(directory, s.history) if s.history else None
        seq = 0
        while True:
            frame = stream.next()
            if frame.received <= ready:
                continue
            seq += 1
            if seq == 1 or seq % 5 == 0:
                observed = focus.read()
                if observed != state:
                    echo(f"focus changed while streaming: {observed}")
                state = observed
            width, height = png_size(frame.png)
            meta = {"captured_utc": frame.captured.isoformat(), "focus_requested": s.focus,
                    "focus_readback": state.as_json(), "frame": seq, "device": s.device,
                    "requested_size": s.size, "width": width, "height": height,
                    "sha256": hashlib.sha256(frame.png).hexdigest()}
            js = (json.dumps(meta, indent=2) + "\n").encode()
            atomic_write(s.latest, frame.png)
            atomic_write(meta_path, js)
            if ring:
                ring.put(seq, {"latest.png": frame.png, "latest.json": js})
            if seq == 1:
                echo(f"first frame in {s.latest}")


def _frame_number(meta: Path) -> int:
    try:
        return int(json.loads(meta.read_text())["frame"])
    except (OSError, ValueError, KeyError, TypeError):
        return -1


def fresh_frame(latest: Path, frames: int = 2, timeout: float = FRAME_TIMEOUT_S) -> bytes:
    """The publisher's frame after FRAMES more: one taken after now, whole"""
    meta = latest.with_suffix(".json")
    start = _frame_number(meta)
    if start < 0:
        raise FocusError(f"no publisher frames in {latest} (arb webcam serve)")
    end = time.monotonic() + timeout
    while _frame_number(meta) < start + frames:
        if time.monotonic() > end:
            raise FocusError(f"the publisher stopped: no new frame in {timeout:.0f} s")
        time.sleep(0.2)
    return latest.read_bytes()


def sweep(focus: Focus, latest: Path, requested: int, out: Path,
          echo: Callable[[str], object] = print) -> None:
    """Frames at both ends of the range, a near setting and REQUESTED (twice:
    approached from each side), from the running publisher; leaves the lens
    at REQUESTED"""
    r = focus.range
    out.mkdir(parents=True, exist_ok=True)
    try:
        for i, v in enumerate([r.minimum, min(60, r.maximum), requested, r.maximum,
                               requested], 1):
            state = focus.apply(v, settle=2)
            dst = out / f"focus-{i:02d}-{v}.png"
            dst.write_bytes(fresh_frame(latest))
            echo(f"{dst}: {state}")
    finally:
        focus.apply(requested, settle=2)
