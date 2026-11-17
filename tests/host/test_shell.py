"""The dev shell's reply loop (shell.py) on a port that hung up."""

from __future__ import annotations

import os
import socket
import time

from auto_rockbox.shell import Shell


def test_hung_up_port() -> None:
    """A port whose other end is gone reads empty and selects readable for
    ever: the reply ends at once, timed out, instead of spinning"""
    ours, theirs = socket.socketpair()
    os.write(theirs.fileno(), b"left over\n")
    theirs.close()
    t0 = time.monotonic()
    r = Shell("fake", "0", "0")._talk(ours.fileno(), "health", 30, None)
    ours.close()
    assert r.timed_out and not r.ok and time.monotonic() - t0 < 5
