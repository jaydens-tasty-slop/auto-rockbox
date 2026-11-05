"""The YP3: a cheap SL6801 MP3 player (two Cortex-M4 cores, 512 KiB SRAM,
2 MiB SPI NOR run in place, SD card, 128x160 GC9106 colour LCD).

The host reaches it through the boot ROM's USB recovery mode (rom.py): a run
writes the changed sectors of a full flash image (image.py) and resets into
it. A YP3_DEV build arms the recovery watchdog at its first instruction and
stops feeding it after the run's return time, so the player comes back to
ROM USB by itself about 17 s later, with its dev log in the SRAM a watchdog
reset keeps (retained.py). With USB on, the running build has a dev shell on
its USB serial port.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from ... import card, keys
from ...elf import Symbolizer
from ...registry import register_target
from ...shell import Shell, find_tty
from ...target import Battery, Lcd, RunRequest, Target
from ...workspace import ConfigError, Workspace
from .image import ImageError, dev_image, parse_push
from .power import guard
from .retained import RETAINED, Names, annotate_ticks, decode_log
from .rom import (
    IMAGE_SIZE,
    ROM_PID,
    ROM_VID,
    DeviceError,
    Op,
    Rom,
    UsbId,
    build_payload,
    rom_devices,
    wait_rom,
)

SHELL_VID, SHELL_PID = "1209", "0001"   # the dev build's USB IDs (yp3.h USB_VENDOR_ID, pid.codes)
CHARGE_LIMIT_MV = 4180                  # the charge guard's default


@dataclass
class Yp3Run(RunRequest):
    """A run with the YP3's extras: files for the card, more flash content,
    and leave to flash an ELF that is not a dev build"""

    pushes: list[tuple[Path, str]] = field(default_factory=list)    # (local file, card path)
    blobs: list[tuple[Path, int]] = field(default_factory=list)     # (file, flash offset)
    release: bool = False


@register_target
class Yp3(Target):
    """SL6801 player, flashed through its boot ROM's USB recovery mode.

    Config ([targets.yp3] in auto-rockbox.toml):
        smtlink        the smtlink_dump binary (the smartlink_flash project) that
                       talks to ROM USB; required by every command that does
        llvm = ""      prefix of clang, ld.lld and llvm-objcopy, which build the
                       service payload (e.g. "/usr/lib/llvm-18/bin/"); "" = PATH
        base_image = "<paths.build>/base.bin"
                       a read-back of this player's whole flash (it holds the
                       stock bootloader); read from the player on first use
        stock_image    a dump of this player's stock firmware, for `stock` (its
                       first 2 MiB); no default
        elf = "<paths.build>/yp3/rockbox.elf"
                       the ELF that names addresses in log, fault, threads, sym,
                       profile and shell traces by default; mass storage boots it
        modules = []   run/flash --modules: "BUILD_PATH=/card/path" pairs, BUILD_PATH
                       relative to the ELF's build directory; missing files skipped
        charge_limit_mv = 4180
                       before a run the charger goes off at or above this
        battery_log = "<paths.build>/battery.log"
                       a line per battery reading and charger change
        card_uuid      the card's filesystem UUID for mass storage (default: the
                       one Rockbox USB disk)
        lock, shell_lock
                       the device and dev-shell lock files in the temp dir
                       (default: auto-rockbox-yp3[-shell].lock); "yp3dev.lock" and
                       "yp3dev-sh.lock" make arb and yp3dev exclude each other
    """

    name = "yp3"
    summary = "SL6801, 2 MiB NOR (XIP), 128x160 LCD; flashed from boot ROM USB 301a:2800"
    lcd = Lcd(128, 160, "rgb565-swapped")
    key_letters = "UDLRS"
    key_suffixes = "+*~"        # + held 1 s, * held 1.2 s as a real key, ~ a real short tap
    keys_max = 255              # apps/yp3_dev.c keys_script[256]
    capabilities = frozenset({"shell", "battery", "usb", "storage"})

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self._previous: UsbId | None = None     # the ROM device the last boot left
        self._elf: Path | None = None           # the ELF the last launch ran
        self._stays_up = False                  # the last launch never returns (return=0)

    # Paths and tools from the config

    @property
    def default_elf(self) -> Path:
        return self.conf.opt_path("elf") or self.ws.build_dir / "yp3" / "rockbox.elf"

    @property
    def battery_log(self) -> Path:
        return self.conf.path("battery_log", str(self.ws.build_dir / "battery.log"))

    def symbolizer(self, elf: Path | None = None) -> Symbolizer:
        return Symbolizer(elf or self._elf or self.default_elf, self.ws.tool("addr2line"))

    def function_names(self, elf: Path | None = None) -> Names:
        """Just the function of each address (addr2line gives "function file:line")"""
        names = self.symbolizer(elf).names
        return lambda addrs: [n.split(" ", 1)[0] for n in names(addrs)]

    @cached_property
    def _transport(self) -> Rom:
        if "smtlink" not in self.conf.data:
            raise ConfigError(f"[targets.{self.name}] smtlink: the path of the smtlink_dump "
                              "binary (smartlink_flash) is needed to reach ROM USB")
        tool = self.conf.path("smtlink")
        if not tool.is_file():
            raise ConfigError(f"[targets.{self.name}] smtlink: {tool} does not exist")
        llvm = self.conf.string("llvm", "")
        return Rom(tool, lambda: build_payload(self.ws.build_dir / "arb" / self.name, llvm))

    # ROM USB

    def rom(self) -> Rom:
        """The player in ROM USB; the caller holds the device lock"""
        if not rom_devices():
            raise DeviceError(f"no ROM USB device ({ROM_VID}:{ROM_PID}): run a dev build that "
                              "returns, or hold Down and press RESET")
        return self._transport

    @contextmanager
    def session(self) -> Generator[Rom]:
        with self.lock():
            yield self.rom()

    def to_rom(self, timeout: float = 120) -> UsbId:
        """Into ROM USB: a running dev build is asked through its shell (`rom`,
        `!rom` while a command blocks the shell); else waits for a run's return
        or Down+RESET"""
        if find_tty(SHELL_VID, SHELL_PID):
            sh = self.shell()
            if "busy" in sh("rom", timeout=30).text:
                sh("!rom", timeout=30)
        ident = wait_rom(time.monotonic() + timeout)
        if ident is None:
            raise DeviceError(f"the player did not come to ROM USB in {timeout:.0f} s")
        return ident

    # Images

    def base_image(self, rom: Rom | None = None) -> bytes:
        """This player's flash as read back once; with ROM, read now if missing"""
        path = self.conf.path("base_image", str(self.ws.build_dir / "base.bin"))
        if not path.exists():
            if rom is None:
                raise ImageError(f"{path}: no base image (a read-back of this player's flash); "
                                 f"`arb yp3 read-flash 0 {IMAGE_SIZE:#x} {path}` makes one")
            print(f"reading the base image from the player into {path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(rom.read_flash(0, IMAGE_SIZE))
        return path.read_bytes()

    def image(self, path: Path, config: Mapping[str, str], *,
              pushes: Sequence[tuple[Path, str]] = (), blobs: Sequence[tuple[Path, int]] = (),
              release: bool = False, rom: Rom | None = None) -> bytes:
        """An ELF placed into the base image with CONFIG, PUSHES and BLOBS; any
        other file is a whole flash image, flashed as it is"""
        if path.suffix == ".elf":
            return dev_image(path, self.base_image(rom), config, pushes=pushes, blobs=blobs,
                             release=release)
        if pushes or blobs:
            raise ImageError(f"{path}: a whole flash image takes no --push, --modules or --blob")
        data = path.read_bytes()
        if len(data) != IMAGE_SIZE:
            raise ImageError(f"{path}: {len(data)} bytes, a flash image is {IMAGE_SIZE}")
        return data

    def modules(self, elf: Path) -> list[tuple[Path, str]]:
        """The configured modules built along with ELF, as pushes"""
        specs = self.conf.get("modules", [])
        if not isinstance(specs, list) or not all(isinstance(s, str) for s in specs):
            raise ConfigError(f"[targets.{self.name}] modules: a list of "
                              "\"BUILD_PATH=/card/path\" strings is needed")
        build = elf.resolve().parent
        pushes = [parse_push(s) for s in specs]
        return [(build / local, sd) for local, sd in pushes if (build / local).exists()]

    @staticmethod
    def run_config(req: RunRequest) -> dict[str, str]:
        """The dev config lines for REQ; an explicit usb= setting beats --usb"""
        if clash := {"return", "keys"} & req.settings.keys():
            raise ImageError(f"--set {'/'.join(sorted(clash))}: the return time is the run's "
                             "SECONDS and the key script its --keys")
        cfg = {"return": str(req.seconds)}
        if req.keys:
            cfg["keys"] = req.keys
        if req.usb:
            cfg["usb"] = "1"
        return cfg | req.settings

    # The Target API

    def launch(self, req: RunRequest) -> None:
        extra = req if isinstance(req, Yp3Run) else Yp3Run(req.image, req.seconds)
        rom = self.rom()
        rom.flash(self.image(req.image, self.run_config(req), pushes=extra.pushes,
                             blobs=extra.blobs, release=extra.release, rom=rom))
        self._previous = rom.reset(Op.BOOT)
        self._elf = req.image if req.image.suffix == ".elf" else None
        self._stays_up = req.seconds == 0
        print("booted" + ("" if self._stays_up else "; waiting for the return to ROM USB"))

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        if self._stays_up:
            return False
        return wait_rom(deadline, self._previous, tick) is not None

    def read_retained(self) -> bytes:
        return self.rom().read_ram(*RETAINED)

    def read_log(self, elf: Path | None = None) -> str:
        return decode_log(self.read_retained(), self.function_names(elf))

    def before_run(self, req: RunRequest) -> None:
        print(_describe(self._guard(stop=False)))

    def after_run(self, req: RunRequest, returned: bool) -> None:
        if returned:
            print(_describe(self._guard(stop=True)))

    def battery(self) -> Battery:
        return self._guard(stop=False)

    def key_duration_ms(self, script: str) -> int:
        """apps/yp3_dev.c holds a key 1 s for any number of +, 1.2 s for *"""
        hold = {"+": 1000, "*": 1200}
        return sum(int(t) if t.isdigit() else keys.TAP_MS + hold.get(t[-1], 0)
                   for t in keys.tokens(script))

    def _guard(self, stop: bool) -> Battery:
        return guard(self.rom(), self.conf.integer("charge_limit_mv", CHARGE_LIMIT_MV),
                     self.battery_log, stop)

    def shell(self) -> Shell:
        names = self.function_names()
        return Shell(self.name, SHELL_VID, SHELL_PID,
                     annotate=lambda line: annotate_ticks(line, names),
                     lock_file=self.conf.string("shell_lock", "") or None)

    @contextmanager
    def mass_storage(self, seconds: int, image: Path | None = None) -> Generator[Path]:
        """Boots IMAGE (default: config `elf`) with usb=storage (speaker off)
        for SECONDS"""
        req = RunRequest(image or self.default_elf, seconds,
                         settings={"usb": "storage", "speaker": "0"})
        self.to_rom()
        uuid = self.conf.string("card_uuid") if "card_uuid" in self.conf.data else None
        with self.lock():
            self.before_run(req)
            self.launch(req)
            t0, booted = time.monotonic(), self._previous
            try:
                yield card.wait(uuid, timeout=240,
                                alive=lambda: all(d == booted for d in rom_devices()))
            finally:
                returned = self.wait_return(t0 + seconds + self.return_margin, lambda: None)
                self.after_run(req, returned)


def _describe(b: Battery) -> str:
    return (f"battery {b.millivolts} mV, {'charging' if b.charging else 'not charging'}"
            + (f"; {b.note}" if b.note else ""))


from . import commands  # noqa: E402, F401  (registers the YP3's commands; they use Yp3)
