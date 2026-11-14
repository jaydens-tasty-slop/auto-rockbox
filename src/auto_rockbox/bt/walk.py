"""The headset walk's live log (`arb TARGET bt-walk`): the player stays on the
desk on USB streaming to headphones that a person carries around, and this PC
holds the dev shell for the whole walk. Every line the player sends (a dev
build mirrors each dev log line to its shell) goes into OUT/walk.txt with a
wall-clock stamp, with the Bluetooth and audio state as it changes and the
link stats polled every few seconds (where the player has them: the YP3's
own link controller does, a controller in ROM may not); the player's own
10 s media stats lines (bitpool, ACL buffer waits, clock restarts) come by
themselves.

The walk ends when the headset is back after a drop and playback resumed
(or did not within RESUME_S), or at the time limit. A USB unplug at the desk
(does the cable matter?) is logged and waited out; lines logged meanwhile
are only in the player's ring and card log.
"""

from __future__ import annotations

import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..shell import SessionLost, Shell
from . import rundir

# The dev shell commands a walk sends, and what their replies start with
STATUS = "bt stack status"      # bt stack: status N sink N, ...; bt connected 0: ADDR cod .. "NAME"
AUDIO = "status"                # audio N elapsed ...
LINK = "bt ctrl link"           # bt link 0: ROLE rssi N dBm txpwr N types ...

STATE_S = 1.5                   # state poll period
REPLY_S = 10.0                  # a reply not ended by then is given up
RESUME_S = 20.0                 # after the headset is back
BT_CONNECTED, BT_STREAMING = 2, 3          # bt_status(): off, on, connected, streaming
AUDIO_PLAYING = 1                          # audio_status(): 1 play, 3 play + pause

_BT = re.compile(r"bt stack: status (\d+)")
_AUDIO = re.compile(r"audio (\d+) elapsed")
_DEVICE = re.compile(r"bt connected \d+: ")


class Session(Protocol):
    def send(self, line: str) -> None: ...
    def read(self, timeout: float) -> list[str]: ...
    def reopen(self) -> bool: ...


@dataclass
class Walk:
    """The walk's state and its end conditions, fed the player's lines"""

    log: rundir.Log
    seconds: float
    link_period: float = 5.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    bt: int = -1
    audio: int = -1
    device: str = ""
    dropped: bool = False
    back_at: float | None = None

    def run(self, session: Session) -> str:
        """Polls and logs until an end; the end's description"""
        now = self.clock()
        end = now + self.seconds
        period = {STATUS: STATE_S, AUDIO: STATE_S, LINK: self.link_period}
        due = dict.fromkeys(period, now)
        queue: deque[str] = deque()
        pending: str | None = None
        sent = 0.0
        while (now := self.clock()) < end:
            for cmd, t in due.items():
                if now >= t and cmd not in queue and cmd != pending:
                    queue.append(cmd)
                    due[cmd] = now + period[cmd]
            if pending and now - sent > REPLY_S:
                self.log(f"walk: no reply to {pending!r}")
                pending = None
            try:
                if pending is None and queue:
                    pending = queue.popleft()
                    session.send(pending)
                    sent = now
                lines = session.read(0.2)
            except SessionLost:
                if not self._usb_gone(session, end):
                    break
                queue.clear()
                pending = None
                continue
            for line in lines:
                if line == "ok" or line in (f"err {pending}", "err busy"):
                    if line != "ok":
                        self.log(f"walk: {line}")
                    if line == f"err {LINK}":       # a player without the link stats
                        del due[LINK]
                    pending = None
                else:
                    self._line(line)
            if outcome := self._outcome():
                self.log(f"walk: {outcome}")
                return outcome
        self.log("walk: time up")
        return "time up"

    def _usb_gone(self, session: Session, end: float) -> bool:
        """Waits for the port to come back; False if the time ran out"""
        self.log("walk: usb gone")
        while self.clock() < end:
            if session.reopen():
                self.log("walk: usb back")
                return True
            self.sleep(1.0)
        return False

    def _line(self, line: str) -> None:
        if m := _BT.match(line):
            self._state(int(m.group(1)), self.audio)
        elif m := _AUDIO.match(line):
            self._state(self.bt, int(m.group(1)))
        elif _DEVICE.match(line):
            if line != self.device:
                self.device = line
                self.log(line)
        else:
            self.log(line)

    def _state(self, bt: int, audio: int) -> None:
        if (bt, audio) == (self.bt, self.audio):
            return
        was = self.bt
        self.bt, self.audio = bt, audio
        if audio < 0:                   # the first poll's audio half is still to come
            return
        self.log(f"walk: bt {bt} audio {audio}")
        if was >= BT_CONNECTED > bt:
            self.dropped, self.back_at = True, None
            self.log("walk: dropped")
        elif self.dropped and self.back_at is None and was < BT_CONNECTED <= bt:
            self.back_at = self.clock()
            self.log("walk: headset back")

    def _outcome(self) -> str | None:
        if self.back_at is None:
            return None
        if self.bt == BT_STREAMING and self.audio == AUDIO_PLAYING:
            return "resumed"
        if self.clock() - self.back_at > RESUME_S:
            return f"not resumed: bt {self.bt} audio {self.audio}"
        return None


def walk(shell: Shell, out: Path, minutes: float, link_period: float = 5.0,
         echo: Callable[[str], object] | None = print) -> str:
    """The headset walk into OUT/walk.txt; how it ended"""
    with rundir.Log(out / rundir.WALK, echo) as log, shell.hold() as session:
        log.comment(f"headset walk: up to {minutes:g} min, {LINK!r} every {link_period:g} s")
        return Walk(log, minutes * 60, link_period).run(session)
