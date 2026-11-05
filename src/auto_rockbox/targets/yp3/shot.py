"""A screenshot of the running player through its dev shell: `shot` prints the
framebuffer as rows `fb Y HEX` (the row's bytes as they are in memory), and
`shot Y0 Y1` prints rows Y0..Y1 again. The serial link loses a chunk now and
then when this PC is slow to read, so missing rows are fetched again.

A sleeping LCD shows the frame from before it slept: wake it with a key first.
The SRAM framebuffer is not read after a watchdog return instead: the ROM
reuses SRAM next to it (the scheduler tables just below hold ROM strings
then), so whether the picture survives is unproven.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress

from PIL.Image import Image

from ... import fb
from ...shell import Reply
from ...target import Lcd

Send = Callable[[str], Reply]      # one shell command line, its reply


class ShotError(Exception):
    pass


def parse_rows(lines: Iterable[str], rows: dict[int, list[bytes]]) -> None:
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and parts[0] == "fb" and parts[1].isdigit():
            with suppress(ValueError):      # a row cut mid-line
                rows.setdefault(int(parts[1]), []).append(bytes.fromhex(parts[2]))


def missing_runs(missing: list[int]) -> list[tuple[int, int]]:
    """Consecutive rows as (first, last) pairs"""
    runs: list[tuple[int, int]] = []
    for y in missing:
        if runs and runs[-1][1] == y - 1:
            runs[-1] = (runs[-1][0], y)
        else:
            runs.append((y, y))
    return runs


def screenshot(send: Send, lcd: Lcd, rounds: int = 3) -> tuple[Image, list[int]]:
    """The framebuffer and the rows still missing after ROUNDS refetches
    (black in the picture)"""
    rows: dict[int, list[bytes]] = {}
    width = 2 * lcd.width

    def row(y: int) -> bytes | None:
        return next((r for r in rows.get(y, []) if len(r) == width), None)

    reply = send("shot")
    if not reply.ok:
        raise ShotError("shot failed: " + " / ".join(reply.lines[-5:]))
    parse_rows(reply.lines, rows)
    if not rows:
        raise ShotError("no framebuffer rows")
    for _ in range(rounds):
        missing = [y for y in range(lcd.height) if row(y) is None]
        if not missing:
            break
        for a, b in missing_runs(missing):
            parse_rows(send(f"shot {a} {b}").lines, rows)
    missing = [y for y in range(lcd.height) if row(y) is None]
    raw = b"".join(row(y) or bytes(width) for y in range(lcd.height))
    return fb.decode(raw, lcd), missing
