"""The peers' log (stdout and a file, seconds since the start on each line)
and their bookkeeping of controller patches and stack violations."""

from __future__ import annotations

import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("peer")
h4log = logging.getLogger("peer.h4")

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def flat(obj: object) -> str:
    """A Bumble object's (or error's) text on one line, without colours,
    codec byte dumps or enum class names"""
    text = re.sub(r"\s*\n\s*", " | ", _ANSI.sub("", str(obj))).strip()
    text = re.sub(r"service_capabilities_bytes=b(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"),? ?",
                  "", text)
    text = re.sub(r"<[\w.]+\.([\w|]+): -?\d+>", r"\1", text)     # <Enum.NAME: 3> -> NAME
    return re.sub(r",\s*\)", ")", text)


def why(error: BaseException) -> str:
    return flat(error) or type(error).__name__


class _Format(logging.Formatter):
    def __init__(self, t0: float) -> None:
        super().__init__()
        self.t0 = t0

    def format(self, record: logging.LogRecord) -> str:
        msg = _ANSI.sub("", record.getMessage())
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        name = "" if record.name.startswith("peer") else f"[{record.name}] "
        level = "" if record.levelno == logging.INFO else f"{record.levelname}: "
        return f"{record.created - self.t0:9.3f} {level}{name}{msg}"


@contextmanager
def peer_logging(path: Path, verbose: bool = False, trace: bool = False, *,
                 echo: bool = True) -> Generator[None]:
    """The peer's lines (and Bumble's warnings; with VERBOSE its debug log)
    in PATH, and with ECHO on stdout, while the block runs; TRACE:
    controller A's HCI traffic too"""
    root = logging.getLogger()
    saved = (root.level, logging.getLogger("bumble").level, h4log.level)
    fmt = _Format(time.time())
    handlers: list[logging.Handler] = [logging.FileHandler(path, "w")]
    if echo:
        handlers.append(logging.StreamHandler(sys.stdout))
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("bumble").setLevel(logging.DEBUG if verbose else logging.WARNING)
    h4log.setLevel(logging.INFO if trace else logging.WARNING)
    try:
        yield
    finally:
        for h in handlers:
            root.removeHandler(h)
            h.close()
        root.setLevel(saved[0])
        logging.getLogger("bumble").setLevel(saved[1])
        h4log.setLevel(saved[2])


class Tally:
    """A run's controller patches that fired (each logged the first time) and
    the stack's violations: what a real controller or peer would not accept
    (the first five of each kind logged). Any violation fails the run."""

    def __init__(self) -> None:
        self.patches: Counter[str] = Counter()
        self.violations: Counter[str] = Counter()

    def patch(self, key: str, msg: str) -> None:
        if not self.patches[key]:
            log.info("[patch %s] %s", key, msg)
        self.patches[key] += 1

    def violation(self, kind: str, msg: str) -> None:
        self.violations[kind] += 1
        if self.violations[kind] <= 5:
            log.warning("STACK VIOLATION (%s): %s", kind, msg)

    def patches_line(self) -> str:
        return ", ".join(f"{k}x{v}" for k, v in sorted(self.patches.items())) or "none"

    def violations_line(self) -> str:
        return ", ".join(f"{k} {v}" for k, v in self.violations.items())
