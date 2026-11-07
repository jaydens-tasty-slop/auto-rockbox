"""The YP3's torture suite offline: its scenarios (the shared ones and its
own), what it does at each boot, its expected returns and its own replies
(the controller's link table, the FM monitor), on a fake YP3 whose dev shell
lives in memory (tests/torture/fakes.py). No device is touched."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from auto_rockbox import engine
from auto_rockbox.lock import DeviceLock
from auto_rockbox.shell import Shell
from auto_rockbox.suites import rockbox_torture as rt
from auto_rockbox.suites import torture as gen
from auto_rockbox.target import RunRequest
from auto_rockbox.targets.yp3 import Yp3
from auto_rockbox.targets.yp3 import torture as yt
from auto_rockbox.workspace import Workspace
from tests.torture.fakes import PC, PLAYER, DevShell, FakePcLink, fast, step

YP3_WEIGHTS = {"codecs": 12, "skips": 6, "monkey": 8, "plugins": 6, "fs": 4, "db": 4, "usb": 2,
               "pluginsweep": 0, "longplay": 4, "record": 8, "recui": 4, "fm": 6, "sleep": 4,
               "sdodd": 4,
               "meta": 4, "missing": 2, "theme": 4, "menus": 6, "fmui": 4, "dbdeep": 4, "crawl": 16,
               "codecmix": 8, "shuffle": 8, "folders": 4, "decode": 10, "linein": 4,
               "video": 4}
BT_WEIGHTS = {"bt-onoff": 3, "bt-pcdisc": 2, "bt-pcpower": 0, "bt-avrcp": 2, "bt-churn": 2,
              "bt-rate": 2, "bt-disturb": 2, "bt-reboot": 1, "bt-forget": 1, "bt-capture": 2}
BUMBLE_WEIGHTS = {"bt-headset": 2, "bt-hid": 2}
FMMON_0 = ["fmmon: periods=9509 consumed=9508 over=0 under=55 peak 2/174 avg 0/44",
           "mixer chan=1 pcm playing=1 dac periods=77529",
           "mixer gaps: longest 5 ms, 0 over two periods, last call 0 ms ago"]
FMMON_1 = ["fmmon: periods=9736 consumed=9430 over=305 under=1287 peak 552/560 avg 177/179",
           "mixer chan=1 pcm playing=1 dac periods=81320",
           "mixer gaps: longest 19 ms, 74 over two periods, last call 2 ms ago"]


class FakeYp3(Yp3):
    """The YP3 with no transport: a launch boots the shell in memory, ROM
    USB is wherever the shell is not"""

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self.requests: list[RunRequest] = []
        self.dev = DevShell("yp3", "1209", "0001", yt.Yp3Torture.card_dir)

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def before_run(self, req: RunRequest) -> None:
        return None

    def launch(self, req: RunRequest) -> None:
        self.requests.append(req)
        self.dev.boot()

    def shell(self, elf: Path | None = None) -> Shell:
        return self.dev


@contextmanager
def yp3_run(root: Path, pc: FakePcLink | None = None
            ) -> Generator[tuple[FakeYp3, yt.Yp3Torture]]:
    """The YP3 suite started on a fake YP3 (booted, set up, preflight done);
    with PC, linked to that fake PC"""
    conf = {"targets": {"yp3": {"bt_address": PLAYER}}} if pc else {}
    p = FakeYp3(Workspace(root, conf))
    s = yt.Yp3Torture(p, root / "rockbox.elf", seed=1)
    if pc:
        s.pc = p.dev.pc = pc
        p.dev.bt_addr = PC
    with (fast(p.dev.clock), mock.patch.object(yt, "wait_rom",
                                               lambda *a: None if p.dev.up else object()),
          mock.patch.object(rt.bluez, "trust")):
        s.start(root)
        try:
            yield p, s
        finally:
            s.close()


def test_registered_for_yp3() -> None:
    ws = Workspace(Path("/nonexistent"))
    table = engine.scenarios_for(yt.Yp3Torture(Yp3(ws), Path("x.elf")))
    assert {n: s.weight for n, s in table.items()} == YP3_WEIGHTS
    assert {n for n, s in table.items() if s.targets is not None} == {"fm"}
    assert table["fm"].targets == {"yp3"}
    # this PC as the headset only with the player's address configured
    bt = Workspace(Path("/nonexistent"),
                   {"targets": {"yp3": {"bt_address": "aa:bb:cc:dd:ee:ff"}}})
    suite = yt.Yp3Torture(Yp3(bt), Path("x.elf"))
    table = engine.scenarios_for(suite)
    assert {n: s.weight for n, s in table.items()} == YP3_WEIGHTS | BT_WEIGHTS
    assert suite.pc and suite.pc.player == "AA:BB:CC:DD:EE:FF"
    bt = Workspace(Path("/nonexistent"), {"targets": {"yp3": {"bt_address": PLAYER}},
                                          "bluetooth": {"adapter": "usb:10d7:b012"}})
    with mock.patch.object(rt, "have_bumble", return_value=True):
        suite = yt.Yp3Torture(Yp3(bt), Path("x.elf"))
    table = engine.scenarios_for(suite)
    assert {n: s.weight for n, s in table.items()} == YP3_WEIGHTS | BT_WEIGHTS | BUMBLE_WEIGHTS
    assert gen.suite_class("yp3") is yt.Yp3Torture


def test_yp3_boot_and_steps() -> None:
    with tempfile.TemporaryDirectory() as tmp, yp3_run(Path(tmp)) as (p, s):
        # the boot: the settings files pushed with the image (no mass storage
        # boot), the charge limit, the dry power-off, the module probe
        (req,) = p.requests
        assert isinstance(req, yt.Yp3Run) and req.seconds == 0 and req.usb
        assert req.settings == {"speaker": "0"}
        assert sorted(sd for _, sd in req.pushes) == sorted(
            f"/yp3dev/{n}" for n in [*rt.CFGS, "t_limit.cfg"])
        assert (Path(tmp) / "t_limit.cfg").read_text() == "charge limit: 80\n"
        assert {"dryoff", "cfg /yp3dev/t_limit.cfg", f"play {rt.PROBE}"} <= set(p.dev.sent)
        assert s.library_track(album=True) is None      # codectest/ and yp3dev/ are no albums
        assert step(s, "meta") == []
        p.dev.charging, p.dev.level = True, 85
        assert step(s, "meta") == ["charging at 85% (4121 mV), limit 80%"]
        # FM recordings (two in three) tune with the YP3's own command
        for seed in range(4):
            step(s, "record", seed=seed)
        assert any(x.startswith("rec ") and " fm " in x for x in p.dev.sent)
        assert any(re.fullmatch(r"fm \d+", x) for x in p.dev.sent) and "fm off" in p.dev.sent


def test_yp3_expected_return() -> None:
    s = yt.Yp3Torture(Yp3(Workspace(Path("/nonexistent"))), Path("x.elf"))
    assert s.expected_return([("dev log (tail)", "x\npower_off() (dry run: parking)\n")]) == \
        "SHUTDOWN (menu power-off, expected)"
    assert s.expected_return([("dev log (tail)", "HANG: main 12 s\n")]) is None
    assert s.crash_re.search("HANG: main thread stuck") and s.crash_re.search("*PANIC* stkov")


def test_link_table() -> None:
    raw = bytearray(52)
    raw[7:14] = bytes([1, 0x10, 0xEE, 0xDD, 0xCC, 0xBB, 0xAA])     # entry 1, BD_ADDR LSB first
    raw[14:21] = bytes([0, 1, 2, 3, 4, 5, 6])                     # entry 2: not in use
    lines = [f"{yt.BT_LINK_TBL + 4 * i:08x}: {int.from_bytes(raw[4 * i:4 * i + 4], 'little'):08x}"
             for i in range(13)]
    assert yt.link_table(rt.r32_words(lines)) == ["AA:BB:CC:DD:EE:10"]


def test_fm_replies() -> None:
    assert yt.fm_periods(" ".join(FMMON_0)) == (9509, 0)
    assert yt.fm_periods(" ".join(FMMON_1)) == (9736, 305)
    assert yt.fm_consumer(" ".join(FMMON_0), " ".join(FMMON_1)) == (
        "; mixer channel playing, pcm playing 1, DAC periods +3791; longest mixer gap 19 ms, "
        "74 over two periods, last call 2 ms ago")


def test_yp3_bt_fail_details() -> None:
    """A failed setup on the YP3: its LMP trace and link table beside the step"""
    with tempfile.TemporaryDirectory() as tmp, yp3_run(Path(tmp), FakePcLink()) as (p, s):
        p.dev.bt_paired = False
        p.dev.drop()
        assert s.pc is not None
        with mock.patch.object(FakePcLink, "answers", return_value=False):
            problems = step(s, "bt-churn")
        assert problems and problems[0].startswith("bt setup: no connection"), problems
        (fail,) = Path(tmp).glob("*-bt-fail.txt")
        text = fail.read_text()
        assert "--- sh log" in text and "--- sh bt ctrl lmp" in text
        assert "--- link table in use: none" in text
        assert "bt ctrl lmp" in p.dev.sent and f"r32 {yt.BT_LINK_TBL:#x} 13" in p.dev.sent
