"""Files that a long-running publisher writes and other processes poll (the
Line In and webcam publishers, the attention request): readers must never see
half a file, and a history must not grow without bound.
"""

from __future__ import annotations

import fcntl
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path


class PublisherBusy(Exception):
    """Another publisher already writes this directory"""


def atomic_write(path: Path, data: bytes) -> None:
    """Readers see the old content or the new, never a partial file"""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def exclusive(directory: Path) -> Generator[None]:
    """One publisher per directory: two would interleave their sequence
    numbers in the same ring. The lock file is the one the earlier capture
    scripts used, so a new publisher also refuses to run beside an old one."""
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory / ".capture.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublisherBusy(f"another publisher writes {directory}") from None
        yield
    finally:
        os.close(fd)


class Ring:
    """DIR/history/000, 001, ...: a fixed number of slots reused in turn, so
    the history stays bounded and a slot's path never comes from the data"""

    def __init__(self, directory: Path, size: int) -> None:
        if size < 1:
            raise ValueError("a ring needs at least one slot")
        self.root = directory / "history"
        self.size = size

    def index(self, seq: int) -> int:
        """The slot number of sequence number SEQ (counted from 1)"""
        return (seq - 1) % self.size

    def slot(self, seq: int) -> Path:
        return self.root / f"{self.index(seq):03d}"

    def put(self, seq: int, files: dict[str, bytes]) -> Path:
        slot = self.slot(seq)
        slot.mkdir(parents=True, exist_ok=True)
        for name, data in files.items():
            atomic_write(slot / name, data)
        return slot
