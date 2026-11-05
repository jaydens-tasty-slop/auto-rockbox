"""BlueZ and PipeWire helpers with their tools faked: conn-info parsing, the
player's input node, stream routing, and the watch loop's paging."""

from __future__ import annotations

import json
import subprocess
import unittest
from collections.abc import Callable
from unittest import mock

from auto_rockbox.bt import bluez

ADDR = "AA:BB:CC:DD:EE:01"
OTHER = "AA:BB:CC:DD:EE:02"


def done(stdout: str, rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout, "")


class ConnInfoTest(unittest.TestCase):
    def test_parse(self) -> None:
        ok = (f"Connection Information for {ADDR} (BR/EDR) \tRSSI -13\tTX power 4\t"
              "maximum TX power -24 ")
        assert bluez.parse_conn_info(ok) == bluez.ConnInfo(-13, 4, -24)
        failed = (f"\x1b[0;91mGet Conn Info for {ADDR} (BR/EDR) failed. status 0x02 "
                  "(Not Connected) \x1b[0m")
        assert bluez.parse_conn_info(failed) is None

    def test_rssi_line_round_trip(self) -> None:
        c = bluez.ConnInfo(-7, 0, 4)
        assert bluez.parse_rssi_line(c.line()) == c
        assert bluez.parse_rssi_line("none") is None


class PipeWireTest(unittest.TestCase):
    def node(self, name: str, api: str, cls: str, addr: str) -> dict[str, object]:
        return {"type": "PipeWire:Interface:Node",
                "info": {"props": {"node.name": name, "device.api": api, "media.class": cls,
                                   "api.bluez5.address": addr}}}

    def test_input_node(self) -> None:
        dump = json.dumps([
            {"type": "PipeWire:Interface:Client", "info": {"props": {}}},
            self.node("alsa_input.mic", "alsa", "Audio/Source", ""),
            self.node("bluez_output.x.1", "bluez5", "Audio/Sink", ADDR),
            self.node("bluez_input.other.2", "bluez5", "Audio/Source", OTHER),
            self.node(f"bluez_input.{ADDR.replace(':', '_')}.2", "bluez5", "Audio/Source",
                      ADDR),
        ])
        with mock.patch.object(bluez, "_run", return_value=done(dump)):
            assert bluez.input_node(ADDR) == "bluez_input.AA_BB_CC_DD_EE_01.2"
            assert bluez.input_node() == "bluez_input.other.2"
            assert bluez.input_node("AA:BB:CC:DD:EE:03") is None
        with mock.patch.object(bluez, "_run", return_value=None):
            assert bluez.input_node(ADDR) is None

    def test_route(self) -> None:
        calls: list[list[str]] = []

        def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            if argv[:3] == ["pactl", "list", "short"]:
                return done("1\talsa_output.x\tmodule\n7\tarb_null\tmodule\n")
            if argv[:2] == ["pactl", "-f"]:
                return done(json.dumps([
                    {"index": 40, "sink": 1, "properties": {
                        "node.name": f"bluez_input.{ADDR.replace(':', '_')}.2"}},
                    {"index": 41, "sink": 1, "properties": {"node.name": "firefox"}},
                    {"index": 42, "sink": 7, "properties": {"api.bluez5.address": ADDR}},
                ]))
            return done("")

        with mock.patch.object(bluez, "_run", run):
            bluez.route(ADDR, "arb_null")
        moves = [c for c in calls if c[:2] == ["pactl", "move-sink-input"]]
        assert moves == [["pactl", "move-sink-input", "40", "arb_null"]]


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.now += s


class WatchTest(unittest.TestCase):
    def watch(self, states: list[bool | None], page_for: float,
              stop: Callable[[str], bool]) -> list[tuple[str, bool]]:
        """The events until STOP says so; connected() answers STATES in turn"""
        answers = iter(states)
        clock = FakeTime()
        pages: list[str] = []

        def connect(addr: str, timeout: float = 0) -> bool:
            pages.append(addr)
            clock.sleep(5)
            return False

        out = []
        with (mock.patch.object(bluez, "connected", lambda a: next(answers)),
              mock.patch.object(bluez, "connect", connect),
              mock.patch.object(bluez, "time", clock)):
            for ev in bluez.watch_link(ADDR, page_for=page_for):
                out.append((ev.kind, ev.connected))
                if stop(ev.kind):
                    break
        self.pages = pages
        return out

    def test_drop_and_back(self) -> None:
        ev = self.watch([False, True, True, False, False, True], 300, lambda k: k == "back")
        assert ev == [("poll", False), ("poll", True), ("up", True), ("poll", True),
                      ("poll", False), ("down", False), ("page", False), ("poll", False),
                      ("page", False), ("poll", True), ("back", True)]
        assert self.pages == [ADDR, ADDR]

    def test_over(self) -> None:
        ev = self.watch([True, False] + [False] * 100, 60, lambda k: False)
        assert ev[-1] == ("over", False)
        # a page (5 s) every 25 s from the drop; the one 75 s after it ends past 60
        assert [k for k, _ in ev].count("page") == 4

    def test_no_paging(self) -> None:
        ev = self.watch([True, False, False, None, True], 0, lambda k: k == "back")
        assert [k for k, _ in ev] == ["poll", "up", "poll", "down", "poll", "poll", "poll",
                                      "back"]
        assert self.pages == []


if __name__ == "__main__":
    unittest.main()
