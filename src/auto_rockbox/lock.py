"""One process at a time per device: a bench has one cable per player, and two
tools talking to it at once corrupt both conversations."""

from __future__ import annotations

import fcntl
import os
import tempfile
import time
from pathlib import Path
from types import TracebackType

from .errors import ArbError


class DeviceBusy(ArbError):
    pass


class DeviceLock:
    """An flock on a per-device file in the temp dir; waits up to TIMEOUT s.
    FILE names another lock file (relative to the temp dir): a tool that
    flocks the same file then excludes arb and the other way round."""

    def __init__(self, name: str, timeout: float = 600.0, file: str | None = None) -> None:
        self.path = Path(tempfile.gettempdir()) / (file or f"auto-rockbox-{name}.lock")
        self.timeout = timeout
        self.fd: int | None = None

    def __enter__(self) -> DeviceLock:
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        end = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > end:
                    os.close(self.fd)
                    self.fd = None
                    raise DeviceBusy(f"{self.path} is held by another process") from None
                time.sleep(0.2)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, f"{os.getpid()}\n".encode())
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
