"""The headset walk's logger against a scripted dev shell: what it writes,
what it keeps out of the log, and each way a walk ends."""

from __future__ import annotations

import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path

from auto_rockbox.bt import rundir, walk

MEDIA = "bt: media 430 pkts bp 53 behind 0 ms waits 0 restarts 0 bufs 9/9"
LINK = "bt link 0: master rssi -40 dBm txpwr 19 types ee1c pref ff1e lm 05 txen 1"
DEVICE = 'bt connected 0: AA:BB:CC:DD:EE:01 cod 240404 "Phones"'


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class Player:
    """A dev shell: the walk's commands answered from STATES, (from time, bt
    status, audio status) in order; LINES are (time, line) the player logs by
    itself; its port is gone during GONE (from, to)"""

    def __init__(self, clock: Clock, states: list[tuple[float, int, int]],
                 lines: Sequence[tuple[float, str]] = (),
                 gone: tuple[float, float] | None = None) -> None:
        self.clock, self.states, self.gone = clock, states, gone
        self.lines = sorted(lines)
        self.out: list[str] = []
        self.sent: list[str] = []

    def _gone(self) -> bool:
        return self.gone is not None and self.gone[0] <= self.clock.t < self.gone[1]

    def send(self, line: str) -> None:
        if self._gone():
            raise walk.SessionLost("gone")
        self.sent.append(line)
        bt, audio = next((b, a) for t, b, a in reversed(self.states) if t <= self.clock.t)
        if line == walk.STATUS:
            self.out.append(f"bt stack: status {bt} sink 1, irq stack used 1192 of 2048")
            if bt >= 2:
                self.out.append(DEVICE)
        elif line == walk.AUDIO:
            self.out.append(f"audio {audio} elapsed 1000/179252 ms 44100 Hz file /a.mp3")
        elif line == walk.LINK:
            self.out.append(LINK)
        self.out.append("ok")

    def read(self, timeout: float) -> list[str]:
        if self._gone():
            raise walk.SessionLost("gone")
        self.clock.t += timeout
        due = [x for t, x in self.lines if t <= self.clock.t]
        self.lines = [(t, x) for t, x in self.lines if t > self.clock.t]
        out, self.out = self.out + due, []
        return out

    def reopen(self) -> bool:
        self.out = []
        return not self._gone()


class WalkTest(unittest.TestCase):
    def walk(self, player: Player, clock: Clock, seconds: float = 300) -> tuple[str, list[str]]:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / rundir.WALK
            with rundir.Log(path, echo=None) as log:
                end = walk.Walk(log, seconds, 5.0, clock, clock.sleep).run(player)
            return end, [x for _, x in rundir.read(path)]

    def test_drop_and_resume(self) -> None:
        clock = Clock()
        player = Player(clock, [(0, 3, 1), (20, 1, 1), (40, 2, 1), (43, 3, 1)], [
            (5, MEDIA), (20.1, "bt: disconnected: 08"), (25, "bt: link lost: paging again (1)"),
            (40, "bt: connected, handle 128"),
            (43, "bt: streaming, 5 frames of 83 bytes per packet, bitpool 35")])
        end, lines = self.walk(player, clock)
        assert end == "resumed"
        notes = [x for x in lines if x.startswith("walk: ")]
        assert notes == ["walk: bt 3 audio 1", "walk: bt 1 audio 1", "walk: dropped",
                         "walk: bt 2 audio 1", "walk: headset back", "walk: bt 3 audio 1",
                         "walk: resumed"], notes
        assert MEDIA in lines and "bt: disconnected: 08" in lines
        assert lines.count(DEVICE) == 1                # logged when it changes only
        assert lines.count(LINK) >= 8                  # every 5 s for ~43 s
        assert not [x for x in lines if x.startswith(("bt stack: status", "audio "))]
        assert 43 <= clock.t < 46, clock.t

    def test_not_resumed(self) -> None:
        clock = Clock()
        end, lines = self.walk(Player(clock, [(0, 3, 1), (10, 1, 3), (20, 3, 3)]), clock)
        assert end == "not resumed: bt 3 audio 3"
        assert lines[-1] == "walk: not resumed: bt 3 audio 3"
        assert 40 < clock.t < 43

    def test_time_up(self) -> None:
        clock = Clock()
        end, lines = self.walk(Player(clock, [(0, 3, 1)]), clock, seconds=30)
        assert end == "time up" and lines[-1] == "walk: time up"
        assert 30 <= clock.t < 30.5

    def test_usb_gone(self) -> None:
        clock = Clock()
        player = Player(clock, [(0, 3, 1), (30, 1, 1), (50, 3, 1)], gone=(10, 15))
        end, lines = self.walk(player, clock)
        assert end == "resumed"
        assert lines.index("walk: usb gone") < lines.index("walk: usb back")
        assert "walk: dropped" in lines and "walk: headset back" in lines

    def test_one_command_at_a_time(self) -> None:
        """A command goes only after the last one's ok: the shell answers
        "err busy" to one sent meanwhile"""
        class Strict(Player):
            def send(self, line: str) -> None:
                assert self.out == [], f"{line!r} sent before the last reply was read"
                super().send(line)

        clock = Clock()
        player = Strict(clock, [(0, 3, 1)])
        self.walk(player, clock, seconds=20)
        assert player.sent.count(walk.LINK) == 4       # 0, 5, 10, 15 s
        assert player.sent.count(walk.STATUS) >= 10

    def test_no_link_stats(self) -> None:
        """A player whose shell has no link stats (the TS17's controller is
        in ROM): asked once, then the walk goes on without them"""
        class NoLink(Player):
            def send(self, line: str) -> None:
                if line == walk.LINK:
                    self.sent.append(line)
                    self.out.append(f"err {line}")
                else:
                    super().send(line)

        clock = Clock()
        player = NoLink(clock, [(0, 3, 1), (20, 1, 1), (40, 3, 1)])
        end, lines = self.walk(player, clock)
        assert end == "resumed"
        assert player.sent.count(walk.LINK) == 1
        assert lines.count(f"walk: err {walk.LINK}") == 1


if __name__ == "__main__":
    unittest.main()
