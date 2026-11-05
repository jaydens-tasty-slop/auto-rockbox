"""A range run's directory: the files each tool writes, under fixed names so
`arb bt report` finds them, and the wall-clock stamp that starts each line.

    walk.txt     arb yp3 bt-walk: the player's lines, its Bluetooth and audio
                 state, the link polls (headset walk)
    devlog.txt   the player's card log (dev config sdlog=1), copied in after
                 a walk off USB; the only file without stamps
    gaps.txt     arb bt gaplog (live) or arb bt gaps --out (a recording)
    conn.txt     arb bt watch: the link as BlueZ sees it, and pages
    pc-rssi.txt  arb bt capture: the PC's RSSI and TX power for the link
    pc.btsnoop   arb bt capture: the PC's HCI trace (btmon -w)

A stamp is local time with its UTC offset, to the ms (ISO 8601): every file
of a run is written on this PC, and the walker notes local times.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date, datetime
from datetime import time as clock_time
from pathlib import Path
from types import TracebackType

WALK = "walk.txt"
CARD_LOG = "devlog.txt"
GAPS = "gaps.txt"
CONN = "conn.txt"
RSSI = "pc-rssi.txt"
TRACE = "pc.btsnoop"


def now() -> datetime:
    return datetime.now().astimezone()


def stamp(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds")


def clock(t: datetime) -> str:
    """HH:MM:SS.mmm, for the terminal and the report"""
    return t.strftime("%H:%M:%S.%f")[:-3]


def parse_when(text: str, day: date) -> datetime:
    """A local time: ISO 8601 with a date (`stat -c %w` output works too), or
    HH:MM:SS[.fff] on DAY"""
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        try:
            t = datetime.combine(day, clock_time.fromisoformat(text))
        except ValueError:
            raise ValueError(f"{text!r}: HH:MM:SS[.fff] or an ISO date and time") from None
    return t if t.tzinfo else t.astimezone()


class Log:
    """Appends stamped lines to a run file as they happen. Line-buffered: a
    walk ended with Ctrl-C keeps every line. ECHO gets each line with a short
    stamp."""

    def __init__(self, path: Path, echo: Callable[[str], object] | None = print) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.echo = echo
        self._f = path.open("a", buffering=1)

    def __call__(self, text: str, at: datetime | None = None, quiet: bool = False) -> None:
        t = at or now()
        self._f.write(f"{stamp(t)} {text}\n")
        if self.echo and not quiet:
            self.echo(f"{clock(t)} {text}")

    def comment(self, text: str) -> None:
        self._f.write(f"# {text}\n")

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> Log:
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()


def read(path: Path) -> Iterator[tuple[datetime, str]]:
    """The stamped lines of a run file; comments and unstamped lines are skipped"""
    for raw in path.read_text(errors="replace").splitlines():
        head, _, rest = raw.partition(" ")
        if not head or head.startswith("#"):
            continue
        try:
            t = datetime.fromisoformat(head)
        except ValueError:
            continue
        yield (t if t.tzinfo else t.astimezone()), rest
