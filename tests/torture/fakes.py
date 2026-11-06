"""Players for the torture tests whose dev shell lives in memory, and a clock
whose sleeps take no time:

    SimPlayer    a generic player that crashes, hangs and powers off on cue
                 (SimSuite, the sim-* scenarios)
    DevShell     a Rockbox dev build's shared shell (apps/dev_shell.c),
                 answering the commands the scenarios under test send as the
                 players do; FakePlayer is a Rockbox player around it, the
                 players' own fakes (tests/yp3, tests/ts17) put it in theirs
    FakePcLink   this PC's Bluetooth side
"""

from __future__ import annotations

import random
import wave
from collections.abc import Callable, Generator
from contextlib import contextmanager, nullcontext
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any, Self
from unittest import mock

import numpy as np

from auto_rockbox import card, engine
from auto_rockbox.engine import StepContext
from auto_rockbox.lock import DeviceLock
from auto_rockbox.shell import NoShell, Reply, Shell
from auto_rockbox.suites import rockbox_torture as rt
from auto_rockbox.suites import torture as gen
from auto_rockbox.target import Lcd, RunRequest, Target
from auto_rockbox.workspace import Workspace

PLAYER, PC = "AA:BB:CC:DD:EE:FF", "00:1A:7D:DA:71:13"
STATUS_STOPPED = "audio 0 elapsed 0/0 ms 0 Hz buffered 0/0 periods 12510 file -"


# ---------------------------------------------------------------- the clock

class Clock:
    """time.sleep and time.monotonic for runs that take no time"""

    def __init__(self) -> None:
        self.now = 1000.0

    def sleep(self, secs: float) -> None:
        self.now += secs

    def monotonic(self) -> float:
        return self.now


@contextmanager
def fast(clock: Clock) -> Generator[None]:
    with mock.patch("time.sleep", clock.sleep), mock.patch("time.monotonic", clock.monotonic):
        yield


def engine_steps(n: int) -> type[datetime]:
    """The engine's clock, a minute later at each look: a run until
    deadline(n) takes N steps"""
    class Ticks(datetime):
        looks = 0

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> Self:
            cls.looks += 1
            return cls(2026, 11, 5) + timedelta(minutes=cls.looks)
    return Ticks


def deadline(n: int) -> datetime:
    return datetime(2026, 11, 5) + timedelta(minutes=n + 2)


# ---------------------------------------------------------------- a generic player

class SimPlayer(Target):
    """A player whose image runs in memory: launched, it answers its shell
    until a command sends it back to the host (`!rom`, `crash`, `poweroff`)"""

    name = "sim"
    summary = "the torture tests' player"
    lcd = Lcd(128, 160)
    key_letters = "UDLRS"
    capabilities = frozenset({"shell"})

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self.up = False
        self.launches: list[RunRequest] = []
        self.log = ""
        self.underruns = 0
        self.free = 100000
        self.sim_shell = SimShell(self)

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def launch(self, req: RunRequest) -> None:
        self.launches.append(req)
        self.up, self.log, self.underruns = True, "", 0

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        tick()
        return not self.up

    def read_log(self, elf: Path | None = None) -> str:
        return self.log

    def shell(self) -> Shell:
        return self.sim_shell


class SimShell(Shell):
    def __init__(self, p: SimPlayer) -> None:
        super().__init__("sim", "0000", "0000")
        self.p = p

    def port(self) -> str:
        if not self.p.up:
            raise NoShell("sim: no shell")
        return "/dev/null"

    def __call__(self, line: str, timeout: float = 120.0,
                 echo: Callable[[str], None] | None = None) -> Reply:
        self.port()
        p = self.p
        if line == "crash":
            p.up, p.log = False, "sim boot\n*PANIC* Stkov main\n"
            return Reply(False, ["half a li"], timed_out=True)
        if line == "poweroff":
            p.up, p.log = False, "sim boot\npower_off() (dry run: parking)\n"
            return Reply(False, [], timed_out=True)
        if line == "hang":
            return Reply(False, [], timed_out=True)
        if line == "!rom":
            p.up = False
        elif line == "glitch":
            p.underruns += 1
        elif line == "leak":
            p.free -= 3000
        elif line == "health reset":
            p.underruns = 0
        if line.startswith("health"):
            return Reply(True, [f"health up 5 s audio 0 underruns {p.underruns}/0 stalls 0 "
                                f"ui_late 0 free {p.free} act 1", "ok"])
        return Reply(True, ["ok"])


@gen.player_suite("sim")
class SimSuite(gen.TortureSuite):
    def settled(self, h: gen.Health) -> bool:
        return h.num("act") == 1

    def expected_return(self, records: gen.Records) -> str | None:
        return "power-off (expected)" if "dry run" in dict(records)["dev log (tail)"] else None


def sim_step(command: str, problem: str | None = None) -> Callable[[SimSuite], str]:
    def step(t: SimSuite) -> str:
        h = t.health()
        t.sh(command)
        if problem:
            t.fail(problem)
        t.check_health(h)
        return command
    return step


for _name, _command in (("sim-ok", "work"), ("sim-glitch", "glitch"), ("sim-leak", "leak"),
                        ("sim-crash", "crash"), ("sim-hang", "hang"),
                        ("sim-poweroff", "poweroff")):
    gen.scenario(_name, target={"sim"})(sim_step(_command))
gen.scenario("sim-fail", target={"sim"})(sim_step("work", "a problem of its own"))


def sim_run(root: Path, only: list[str], steps: int, seed: int = 3
            ) -> tuple[SimPlayer, SimSuite, list[engine.Step]]:
    p = SimPlayer(Workspace(root))
    s = SimSuite(p, root / "rockbox.elf", ledger=root / "ledger.csv", seed=seed)
    clock = Clock()
    with fast(clock), mock.patch.object(engine, "datetime", engine_steps(steps)):
        try:
            done = engine.run(s, engine.RunSettings(root / "run", deadline(steps), seed, only),
                              echo=lambda line: None)
        finally:
            s.close()
    return p, s, done


# ---------------------------------------------------------------- a Rockbox dev build's shell

class DevShell(Shell):
    """The shared dev shell (apps/dev_shell.c) in memory: up from boot() to
    `!rom`; CARD_DIR is the build's dev folder"""

    def __init__(self, name: str, vid: str, pid: str, card_dir: str) -> None:
        super().__init__(name, vid, pid)
        self.card_dir = card_dir
        self.up = False
        self.clock = Clock()
        self.sent: list[str] = []
        self.charging, self.level = False, 70
        self.glitch_on = ""                 # a command that underruns
        self.pc: FakePcLink | None = None   # this PC, for Bluetooth
        self.bt_addr: str | None = None     # linked to
        self.sink = 0                       # enum pcm_sink_ids
        self.bt_paired = True               # the player has this PC's key
        self.reset()

    def reset(self) -> None:
        self.audio, self.path, self.started = 0, "-", 0.0
        self.underruns, self.log = 0, [f"Rockbox on {self.name}"]
        self.msgs: list[str] = []           # "KIND TEXT", numbered from 1

    def boot(self) -> None:
        self.reset()
        self.up = True

    def port(self) -> str:
        if not self.up:
            raise NoShell(f"{self.name}: no shell")
        return "/dev/ttyACM0"

    def __call__(self, line: str, timeout: float = 120.0,
                 echo: Callable[[str], None] | None = None) -> Reply:
        self.port()
        assert len(line) <= rt.SHELL_LINE, line
        self.sent.append(line)
        self.log.append(f"sh: {line}")
        if line == "!rom":
            self.up = False
        if line.split(" ", 1)[0] == self.glitch_on:
            self.underruns += 1
        return Reply(True, [*self.answer(line), "ok"])

    def elapsed(self) -> int:
        return int((self.clock.now - self.started) * 1000) if self.audio == 1 else 0

    def answer(self, line: str) -> list[str]:
        cmd, _, arg = line.partition(" ")
        if cmd == "health":
            if arg == "reset":
                self.underruns = 0
            return [f"health up 30 s audio {self.audio} elapsed {self.elapsed()}/233046 "
                    f"underruns {self.underruns}/0 stalls 0 maxstall 0 ui_late 0 ui_age 6 "
                    "free 18912 cpu 64 act 1 plugin - db 0"]
        if cmd == "play":
            self.audio, self.path, self.started = 1, arg, self.clock.now
        elif cmd == "stop":
            self.audio, self.path = 0, "-"
        elif cmd == "status":
            return [f"audio {self.audio} elapsed {self.elapsed()}/233046 ms 44100 Hz buffered "
                    f"40968/40968 periods 0 file {self.path}" if self.audio else STATUS_STOPPED]
        elif cmd == "log":
            return self.log[-40:] + (["bt: bitpool 53"] if self.bt_addr else [])
        elif cmd == "msgs":
            return ([f"msg {i} {m}" for i, m in enumerate(self.msgs, 1) if i > int(arg)]
                    + [f"msgs next {len(self.msgs) + 1}"])
        elif cmd == "bat":
            return [f"battery 4121 mV {self.level}% charging={int(self.charging)} power=1"]
        elif line == "ls /codectest":
            return ["ls /codectest:", "  flac_16_44.flac 1234", "  mp3_320.mp3 5678",
                    "  wv_24_44.wv 999", "  readme.txt 10"]
        elif line == "ls /":
            return ["ls /:", "  Whistle Remix.mp3 11221382", "  codectest/ 0",
                    f"  {self.card_dir.strip('/')}/ 0"]
        elif cmd == "meta":
            return ["meta: 1 in 62 ms: length 233046 ms, first frame 4728, bitrate 320, "
                    "sd reads 1"]
        elif cmd == "mkcheck":
            return [f"mkcheck: {arg.split()[1]} files ok"]
        elif cmd == "count":
            return [f"count {arg}: 200"]
        elif cmd == "bt":
            return self.bt(arg.removeprefix("stack "))
        elif line == "set bluetooth":
            return ["bluetooth: on"]
        elif cmd == "keys" and "S" in arg.split() and self.audio == 3:
            self.audio = 1                  # the WPS resumes
        return []

    def bt(self, line: str) -> list[str]:
        """`bt stack ...`: a link to this PC needs both keys or its pairing agent"""
        cmd, _, arg = line.partition(" ")
        if cmd == "status":
            status = (3 if self.audio == 1 else 2) if self.bt_addr else 1
            return [f"bt stack: status {status} sink {self.sink}, irq stack used "
                    "968 of 2048"] + ([f"bt connected 0: {self.bt_addr} cod 000000"]
                                      if self.bt_addr else [])
        if cmd == "list":
            return [f'bt paired 0: {PC} "pc"'] if self.bt_paired else []
        if cmd == "forget":
            self.bt_paired = False
            if self.bt_addr == arg:
                self.drop()
        elif cmd == "disconnect":
            self.drop()
        elif cmd == "connect" and self.pc and self.pc.answers(self.bt_paired):
            self.bt_addr, self.bt_paired = arg, True
            self.sink = 1                   # the policy routes to the headset
        elif cmd == "sink":
            self.sink = int(arg == "bt")
        return []

    def drop(self) -> None:
        self.bt_addr, self.sink = None, 0
        if self.audio == 1:
            self.audio = 3                  # the policy pauses


class FakePcLink(rt.PcLink):
    """This PC's side in memory: BlueZ keeps the player's key until
    remove(); with no key it takes a page only while its agent runs"""

    def __init__(self, transport: str = "") -> None:
        super().__init__(PLAYER, "arb_null", transport)
        self.knows, self.agent_on, self.agents, self.reclaimed = True, False, 0, 0

    def answers(self, player_paired: bool) -> bool:
        ok = self.agent_on or (self.knows and player_paired)
        self.knows |= ok
        return ok

    @staticmethod
    def ctl(*args: str, timeout: float = 20) -> str:
        return ""                           # never this PC's bluetoothctl

    def find(self) -> bool:
        self.adapter, self.path = PC, "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
        return self.knows

    def powered(self) -> bool:
        return True

    def power(self, on: bool, secs: float = 10) -> bool:
        self.power_state = "on" if on else "off"
        return True

    def others(self) -> list[str]:
        return []

    def connected(self) -> bool:
        return False

    def remove(self) -> None:
        self.knows = False

    @contextmanager
    def agent(self, log: Path, discoverable: bool) -> Generator[None]:
        self.agent_on, self.agents = True, self.agents + 1
        try:
            yield
        finally:
            self.agent_on = False

    def reclaim(self, secs: float = 30) -> bool:
        self.reclaimed += 1
        return True

    def start_routing(self) -> None:
        pass

    def stop_routing(self) -> None:
        pass


# ---------------------------------------------------------------- a Rockbox player

@gen.player_suite("rbfake")
class FakeTorture(rt.RockboxTorture):
    """RockboxTorture with its defaults, as a new player's suite starts"""

    card_dir = "/rbtest"


class FakePlayer(Target):
    """A Rockbox player with no transport: a launch boots the shell in memory,
    the host's mode is wherever the shell is not, and mass storage puts its
    card in a directory (CARD)"""

    name = "rbfake"
    summary = "the torture tests' Rockbox player"
    lcd = Lcd(128, 160)
    key_letters = "UDLRS"
    capabilities = frozenset({"shell", "usb", "storage"})

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self.requests: list[RunRequest] = []
        self.dev = DevShell(self.name, "1209", "00ff", FakeTorture.card_dir)
        self.card = ws.root / "card"
        self.storage_boots: list[Path | None] = []

    def lock(self) -> DeviceLock:
        return DeviceLock(self.name, file=str(self.ws.root / "device.lock"))

    def launch(self, req: RunRequest) -> None:
        self.requests.append(req)
        self.dev.boot()

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        tick()
        return not self.dev.up

    def read_log(self, elf: Path | None = None) -> str:
        return "\n".join(self.dev.log)

    def shell(self) -> Shell:
        return self.dev

    @contextmanager
    def mass_storage(self, seconds: int, image: Path | None = None) -> Generator[Path]:
        """From the host's mode, under the device lock, as every target's"""
        assert not self.dev.up
        with self.lock():
            self.storage_boots.append(image)
            yield Path("/dev/null")


def mounted_at(path: Path) -> Any:
    """card.mounted for a player whose card is PATH"""
    return mock.patch.object(card, "mounted", lambda dev: nullcontext(path))


@contextmanager
def rockbox_run(root: Path, bt_transport: str | None = None
                ) -> Generator[tuple[FakePlayer, FakeTorture, FakePcLink | None]]:
    """FakeTorture started on a FakePlayer (card files put, booted, set up,
    preflight done); with BT_TRANSPORT, linked to a fake PC whose adapter
    is that ("" for none)"""
    conf = {} if bt_transport is None else {"targets": {"rbfake": {"bt_address": PLAYER}}}
    p = FakePlayer(Workspace(root, conf))
    s = FakeTorture(p, root / "rockbox.elf", seed=1)
    pc = None
    if bt_transport is not None:
        pc = FakePcLink(bt_transport)
        s.pc = p.dev.pc = pc
        s.features |= {rt.BT_BUMBLE} if bt_transport else set()
        p.dev.bt_addr, p.dev.sink = PC, 1
    with fast(p.dev.clock), mounted_at(p.card), mock.patch.object(rt.bluez, "trust"):
        s.start(root)
        try:
            yield p, s, pc
        finally:
            s.close()


def step(s: rt.RockboxTorture, name: str, seed: int = 1) -> list[str]:
    """One step as the engine runs it: the problems found"""
    ctx = StepContext(1, name, s.target, s, random.Random(seed), s.out, datetime.now())
    outcome = engine.scenarios_for(s)[name].fn(ctx)
    assert not outcome.lost, outcome
    return outcome.problems + s.check(ctx, outcome)


def tone_wav(path: Path, seconds: float, gap: tuple[float, float] | None = None) -> None:
    """The tone's loud part as this PC records it: 1 kHz left, 1.5 kHz right at
    -15 dBFS; GAP (start s, length s) silent"""
    rate = 44100
    n = np.arange(int(seconds * rate)) / rate
    x = np.stack([np.sin(2 * np.pi * hz * n) for hz in rt.TONE_HZ], axis=1) * 10 ** (-15 / 20)
    if gap:
        x[int(gap[0] * rate):int((gap[0] + gap[1]) * rate)] = 0
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((x * 32767).astype("<i2").tobytes())
