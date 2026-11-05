"""A dev shell over the player's USB serial port (CDC-ACM, Rockbox's own USB
stack): one command line out, its output back up to a closing line `ok` or
`err ...`. The firmware side is a port's dev module; the commands are its own.
"""

from __future__ import annotations

import os
import select
import termios
import time
import tty
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ArbError
from .lock import DeviceLock


def find_tty(vid: str, pid: str) -> str | None:
    """The /dev/ttyACM* of the USB device VID:PID (hex, as in sysfs), or None"""
    for t in sorted(Path("/sys/class/tty").glob("ttyACM*")):
        usbdev = (t / "device").resolve().parent
        try:
            ids = ((usbdev / "idVendor").read_text().strip(),
                   (usbdev / "idProduct").read_text().strip())
        except OSError:
            continue
        if ids == (vid, pid):
            return f"/dev/{t.name}"
    return None


class NoShell(ArbError):
    pass


@dataclass
class Reply:
    ok: bool
    lines: list[str] = field(default_factory=list)
    timed_out: bool = False

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


class Shell:
    def __init__(self, name: str, vid: str, pid: str,
                 annotate: Callable[[str], str] | None = None,
                 lock_file: str | None = None) -> None:
        """ANNOTATE may add to a reply line, e.g. name the addresses in a trace;
        LOCK_FILE as for DeviceLock"""
        self.name, self.vid, self.pid = name, vid, pid
        self.annotate = annotate
        self.lock_file = lock_file

    def port(self) -> str:
        p = find_tty(self.vid, self.pid)
        if p is None:
            raise NoShell(f"{self.name}: no serial port {self.vid}:{self.pid} "
                          "(is the dev build running with USB on?)")
        return p

    def __call__(self, line: str, timeout: float = 120.0,
                 echo: Callable[[str], None] | None = None) -> Reply:
        """Send LINE; the reply's lines, each also passed to ECHO as it comes"""
        with DeviceLock(f"{self.name}-shell", file=self.lock_file):
            fd = os.open(self.port(), os.O_RDWR | os.O_NOCTTY)
            try:
                return self._talk(fd, line, timeout, echo)
            finally:
                os.close(fd)

    def _talk(self, fd: int, line: str, timeout: float,
              echo: Callable[[str], None] | None) -> Reply:
        # A new ttyACM (the player just booted) opens cooked, and log lines
        # the player had queued can be echoed back to its shell before the
        # port is raw: a partial line there would prefix this command. End
        # it with a newline; its "err" goes with the drain below.
        fresh = termios.tcgetattr(fd)[3] & termios.ECHO
        tty.setraw(fd)
        termios.tcflush(fd, termios.TCIOFLUSH)
        if fresh:
            os.write(fd, b"\n")
        # Output left from an earlier command (one that timed out) stays
        # queued in the player until read: its "ok" would end this reply
        while select.select([fd], [], [], 0.3)[0]:
            os.read(fd, 4096)
        os.write(fd, line.encode() + b"\n")
        reply = Reply(False)
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            buf += os.read(fd, 4096)
            *done, buf = buf.split(b"\n")
            for raw in done:
                text = raw.decode(errors="replace")
                if self.annotate:
                    text = self.annotate(text)
                reply.lines.append(text)
                if echo:
                    echo(text)
                if text == "ok" or text.startswith("err"):
                    reply.ok = text == "ok"
                    return reply
        if buf:             # a line cut short: the reply stopped mid-way
            reply.lines.append(buf.decode(errors="replace"))
        reply.timed_out = True
        return reply
