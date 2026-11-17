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
from types import TracebackType

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


class SessionLost(ArbError):
    """The serial port went away (USB unplugged, the player reset)"""


def open_raw(port: str) -> int:
    """PORT opened raw, its queued input dropped. A new ttyACM (the player
    just booted) opens cooked, and log lines the player had queued can be
    echoed back to its shell before the port is raw: a partial line there
    would prefix the next command, so it gets a newline to end it (its
    "err" is read and dropped with the rest)."""
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    fresh = termios.tcgetattr(fd)[3] & termios.ECHO
    tty.setraw(fd)
    termios.tcflush(fd, termios.TCIOFLUSH)
    if fresh:
        os.write(fd, b"\n")
    return fd


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

    def lock(self) -> DeviceLock:
        return DeviceLock(f"{self.name}-shell", file=self.lock_file)

    def hold(self) -> Session:
        """The port held open (with the shell's lock) for a conversation that
        reads everything the player sends: `with shell.hold() as s: ...`"""
        return Session(self)

    def __call__(self, line: str, timeout: float = 120.0,
                 echo: Callable[[str], None] | None = None) -> Reply:
        """Send LINE; the reply's lines, each also passed to ECHO as it comes"""
        with self.lock():
            fd = open_raw(self.port())
            try:
                return self._talk(fd, line, timeout, echo)
            finally:
                os.close(fd)

    def _talk(self, fd: int, line: str, timeout: float,
              echo: Callable[[str], None] | None) -> Reply:
        # Output left from an earlier command (one that timed out) stays
        # queued in the player until read: its "ok" would end this reply. An
        # empty read is a port that hung up (the player left): no reply comes,
        # and select() would keep calling it readable
        while select.select([fd], [], [], 0.3)[0]:
            if not os.read(fd, 4096):
                return Reply(False, timed_out=True)
        os.write(fd, line.encode() + b"\n")
        reply = Reply(False)
        buf, end = b"", time.monotonic() + timeout
        while time.monotonic() < end:
            if not select.select([fd], [], [], 0.2)[0]:
                continue
            if not (data := os.read(fd, 4096)):
                break
            buf += data
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


class Session:
    """The dev shell's serial port held open: commands go out one at a time
    and everything the player sends comes back, replies and mirrored log
    lines alike. (A Shell call opens the port per command and drops what
    came in between.) Holds the shell's lock throughout."""

    def __init__(self, shell: Shell) -> None:
        self.shell = shell
        self.fd: int | None = None
        self._lock = shell.lock()
        self._buf = b""

    def __enter__(self) -> Session:
        self._lock.__enter__()
        try:
            self._open()
        except BaseException:
            self._lock.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        self._close()
        self._lock.__exit__(et, ev, tb)

    def _open(self) -> None:
        self.fd, self._buf = open_raw(self.shell.port()), b""

    def _close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def reopen(self) -> bool:
        """True once the port is back"""
        self._close()
        try:
            self._open()
        except (NoShell, OSError):
            return False
        return True

    def send(self, line: str) -> None:
        if self.fd is None:
            raise SessionLost("port closed")
        try:
            os.write(self.fd, line.encode() + b"\n")
        except OSError as e:
            raise SessionLost(str(e)) from e

    def read(self, timeout: float) -> list[str]:
        if self.fd is None:
            raise SessionLost("port closed")
        try:
            if not select.select([self.fd], [], [], timeout)[0]:
                return []
            data = os.read(self.fd, 4096)
        except OSError as e:
            raise SessionLost(str(e)) from e
        if not data:
            raise SessionLost("end of file")
        *done, self._buf = (self._buf + data).split(b"\n")
        lines = [d.decode(errors="replace").rstrip("\r") for d in done]
        if self.shell.annotate:
            lines = [self.shell.annotate(x) for x in lines]
        return [x for x in lines if x]
