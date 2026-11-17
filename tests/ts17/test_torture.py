"""The TS17's torture suite offline: the shared scenarios without the YP3's
own and without those its build lacks the features for, its dev folder, the
Bluetooth boot setting, and a start on a fake TS17 (the settings files over
mass storage, then the boot with the shell). No device is touched."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from auto_rockbox import engine
from auto_rockbox.lock import DeviceLock
from auto_rockbox.shell import Shell
from auto_rockbox.suites import rockbox_torture as rt
from auto_rockbox.suites import torture as gen
from auto_rockbox.target import RunRequest
from auto_rockbox.targets.ts17 import Ts17
from auto_rockbox.targets.ts17.torture import Ts17Torture
from auto_rockbox.workspace import Workspace
from tests.torture.fakes import DevShell, fast, mounted_at, step

SHARED = {"codecs", "decode", "linein", "video", "codecmix", "shuffle", "skips", "folders",
          "longplay", "theme", "plugins", "db", "record", "fs", "sdodd", "meta", "missing", "usb",
          "pluginsweep", "menus", "monkey", "dbdeep", "recui", "fmui", "crawl", "volume",
          "lcdfill", "hang", "dac"}
NOT_TS17 = {"sleep", "fm"}      # the YP3's LCD gate read and its own FM commands
BT = {"bt-onoff", "bt-pcdisc", "bt-pcpower", "bt-avrcp", "bt-churn", "bt-rate", "bt-disturb",
      "bt-reboot", "bt-forget", "bt-capture"}
ADDRESS = "AA:BB:CC:DD:EE:17"


class FakeTs17(Ts17):
    """The TS17 with no transport: a launch boots the shell in memory,
    download mode is wherever the shell is not, mass storage puts the card
    in a directory"""

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self.requests: list[RunRequest] = []
        self.dev = DevShell("ts17", "1209", "0002", Ts17.dev_dir)
        self.card = ws.root / "card"
        self.storage_boots = 0

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def before_run(self, req: RunRequest) -> None:
        return None

    def launch(self, req: RunRequest) -> None:
        self.requests.append(req)
        self.dev.boot()

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        return not self.dev.up

    def read_log(self, elf: Path | None = None) -> str:
        return "\n".join(self.dev.log)

    def shell(self) -> Shell:
        return self.dev

    @contextmanager
    def mass_storage(self, seconds: int, image: Path | None = None) -> Generator[Path]:
        assert not self.dev.up
        with self.lock():
            self.storage_boots += 1
            yield Path("/dev/null")


def test_suite_for_ts17() -> None:
    assert gen.suite_class("ts17") is Ts17Torture
    s = Ts17Torture(Ts17(Workspace(Path("/nonexistent"))), Path("x.elf"))
    table = engine.scenarios_for(s)
    assert set(table) == SHARED and not set(table) & NOT_TS17
    assert all(x.targets is None for x in table.values())
    assert s.boot_request().settings == {"bluetooth": "off"}
    # with its address: the bt-* scenarios, Bluetooth as the card has it
    ws = Workspace(Path("/nonexistent"), {"targets": {"ts17": {"bt_address": ADDRESS}}})
    s = Ts17Torture(Ts17(ws), Path("x.elf"))
    assert set(engine.scenarios_for(s)) == SHARED | BT
    assert s.pc is not None and s.pc.player == ADDRESS
    req = s.boot_request()
    assert (req.image, req.seconds, req.usb, req.settings) == (Path("x.elf"), 0, True, {})


def test_ts17_card_paths() -> None:
    s = Ts17Torture(Ts17(Workspace(Path("/nonexistent"))), Path("x.elf"))
    assert s.card_path("t_sleep.cfg") == "/ts17test/t_sleep.cfg"
    assert s.tone == "/ts17test/tone.mp3"
    assert s.theme_cfgs[-1] == "/ts17test/t_default.cfg"
    assert set(s.cfgs()) == set(rt.CFGS)            # no charge limit setting in its build


def test_ts17_start() -> None:
    """The settings files into /ts17test over mass storage, then the boot with
    the shell (Bluetooth off) and a step"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        p = FakeTs17(Workspace(root))
        s = Ts17Torture(p, root / "rockbox.elf", seed=1)
        with fast(p.dev.clock), mounted_at(p.card):
            s.start(root)
            try:
                assert p.storage_boots == 1
                assert sorted(f.name for f in (p.card / "ts17test").iterdir()) == sorted(rt.CFGS)
                (req,) = p.requests
                assert req.settings == {"bluetooth": "off"} and req.usb
                assert "dryoff" in p.dev.sent and f"play {rt.PROBE}" in p.dev.sent
                assert s.library_track(album=True) is None      # ts17test/ is no album
                assert step(s, "meta") == []
            finally:
                s.close()
