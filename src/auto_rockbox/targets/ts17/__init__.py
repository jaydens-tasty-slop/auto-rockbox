"""The TS17: an Actions "Lark" player (ATJ2157 family, Cortex-M4F), 4 MiB SPI
NOR run in place through an XIP cache, reached through the boot ROM's ADFU
download mode (USB 10d6:10d6).

The dev loop writes the image over stock's application partition (NOR
0x14000, changed sectors only), writes the run config into an SRAM block
that download mode leaves alone, and starts the image in place from a gadget
(gadget.py). The image returns by a watchdog reset with the boot-mode magic
set, after the run time or a hang, and the log ring is read from the same
SRAM block (devblock.py). With usb=1 the dev shell answers on USB serial
(apps/dev_shell.c, as on every target).
"""

from __future__ import annotations

import re
import struct
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from ... import card, fb
from ...elf import Elf, Symbolizer
from ...registry import register_target
from ...shell import Shell
from ...target import Battery, Lcd, RunRequest, Target
from ...workspace import ConfigError, Workspace
from . import adfu, commands, devblock, gadget, nor, power, rdm, soak, torture
from .adfu import Ts17Error

if TYPE_CHECKING:
    from PIL.Image import Image

__all__ = ["Ts17", "Ts17Error", "commands", "soak", "torture"]

FB_SYMBOL = "lcd_static_framebuffer"    # Rockbox's framebuffer, in SRAM at the gadget's output
FB_KEEP = 0x100                 # the gadget's status block and a few results: what the
                                # charger guard overwrites at the framebuffer's start
SETTLE_S = 0.5                  # download mode answers a moment after it enumerates
SHELL_VID = "1209"              # the dev build's USB vendor (pid.codes test IDs)


# dev-lark.c's last lines before a return: the framebuffer's CRC as memory held it
FB_CRC = re.compile(r"^fb crc ([0-9a-f]{8}) at [0-9a-f]{8}, \d+ bytes$", re.MULTILINE)


def _note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@register_target
class Ts17(Target):
    """Config ([targets.ts17] in auto-rockbox.toml):
        image        the dev build's ELF that `fb` (without one) and the card
                     commands boot (default: <paths.build>/ts17/rockbox.elf)
        card_uuid    the card's filesystem UUID (default: the USB disk with
                     this player's IDs, usb_product)
        stock_cells  a `nor dump --raw` of this unit's whole flash, made before
                     the first run: `nor restore` without an image puts stock's
                     fw0_sys back from it (default: none)

        lock, shell_lock
                     the device and dev-shell lock files in the temp dir
                     (default: auto-rockbox-ts17[-shell].lock); "ts17dev.lock"
                     makes arb and ts17dev exclude each other
        model_tag    the model text at NOR 0x1338 that every write checks first,
                     so the tools never write one Lark player's image into
                     another (default: the class's, from the player's backup)
        bt_address   the player's BD_ADDR: torture's bt-* scenarios (with
                     [bluetooth] sink and adapter); default none
        menu_coverage = "<paths.build>/menu-coverage-ts17.json"
                     the torture crawl's graph of the UI, kept across runs
    Torture: targets/ts17/torture.py, suites/rockbox_torture.py.

    Run settings (--set): those of apps/dev_shell.c (volume=DB, play=,
    bluetooth=off, usb=storage, ...; devblock.SETTINGS), and trace=1, a
    thread dump before the return.
    """

    name = "ts17"
    summary = "Actions Lark (Cortex-M4F), 4 MiB SPI NOR run in place, ADFU download mode"
    lcd = Lcd(128, 160)
    key_letters = "UDLRSVvP"     # ring, centre, volume up/down, on/off
    capabilities = frozenset({"framebuffer", "shell", "battery", "cold", "usb", "storage", "mic"})
    model_tag: ClassVar[str] = "xlx_58209_TS17_01"  # NOR 0x1338 (nor.model_tag)
    usb_product: ClassVar[str | None] = "0002"     # ts17.h
    return_line = devblock.RETURN_LINE
    dev_dir = "/ts17test"           # ts17.h DEV_CARD_DIR
    # a stalled panel; a driver refused a pad another owns (gpio-lark.c)
    log_faults = r"lcd: frame timed out|pad GPIO\d+ REFUSED"
    # the soak's checks are wired (Line In), and a paired PC would take the stream
    soak_settings: ClassVar[dict[str, str]] = {"bluetooth": "off"}

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self._before: tuple[str, str] | None = None   # adfu.present() at the launch
        self._launched = 0.0
        self._cold = False

    def image(self) -> Path:
        return self.conf.opt_path("image") or self.ws.build_dir / self.name / "rockbox.elf"

    def _flat(self, elf: Path) -> bytes:
        out = self.ws.build_dir / "arb" / self.name / "image.bin"
        out.parent.mkdir(parents=True, exist_ok=True)
        gadget.run_tool([self.ws.tool("objcopy"), "-O", "binary", str(elf), str(out)])
        return out.read_bytes()

    def check_unit(self, s: gadget.Session) -> None:
        """Refuses a player whose model text is not this target's: the TS17
        and the TS28 share download mode's USB IDs"""
        want = self.conf.string("model_tag", self.model_tag)
        got = nor.model_tag(s)
        if got != want:
            raise Ts17Error(f"the player in download mode is {got!r}, not the {self.name} "
                            f"({want!r}): another Lark player on USB? Nothing written")

    def before_run(self, req: RunRequest) -> None:
        devblock.check_settings(req.settings)   # bad settings fail before any device call
        devblock.run_config(self.run_config(req))
        with gadget.connect(self.ws) as s:
            _note(power.guard(s).describe())

    def launch(self, req: RunRequest) -> None:
        config = devblock.run_config(self.run_config(req))
        data = self._flat(req.image)
        reset = nor.check_image(data, req.image.name)
        sp = struct.unpack_from("<I", data)[0]
        cells = nor.pad(rdm.encode(nor.pad(data, rdm.XIP_RUN)), nor.SECTOR)
        if len(cells) > nor.FW0_SYS_SIZE:
            raise Ts17Error(f"{req.image.name}: {len(cells):#x} bytes do not fit fw0_sys "
                            f"({nor.FW0_SYS_SIZE:#x})")
        with gadget.connect(self.ws) as s:
            self.check_unit(s)
            n = nor.write_cells(s, nor.FW0_SYS, cells, progress=_note)
            _note(f"{n} of {len(cells) // nor.SECTOR} sectors rewritten")
            s.link.write(devblock.CFG, config)
            s.link.write(devblock.LOG, devblock.EMPTY_LOG_HEADER)
            s.link.write(devblock.CRUMB, bytes(devblock.CRUMB_SIZE))
            self._before = adfu.present()
            s.hand_over(power.reset_ops(download=False) if req.cold else
                        nor.launch_ops(nor.FW0_SYS, reset, devblock.LAUNCH_TOKEN), self._before)
        self._launched = time.monotonic()
        self._cold = req.cold
        _note(f"launched {req.image.name} ({len(data) // 1024} KiB, SP {sp:08x}, "
              f"reset {reset:08x}){' cold' if req.cold else ''}; return after {req.seconds} s")

    def _back(self) -> bool:
        """Download mode is on the bus again, enumerated anew since the launch"""
        now = adfu.present()
        return now is not None and now != self._before

    def wait_return(self, deadline: float, tick: Callable[[], None]) -> bool:
        if self._cold:
            # The boot chain may reuse the dev block: then the image finds no
            # config and returns after its default time
            deadline = max(deadline, self._launched + devblock.DEFAULT_RETURN_S
                           + self.return_margin)
        while time.monotonic() < deadline:
            if self._back():
                time.sleep(SETTLE_S)
                return True
            tick()
            time.sleep(0.2)
        return False

    def read_log(self, elf: Path | None = None) -> str:
        """The dev log; without its return line (a hang the recovery watchdog
        reset), a last line with where the image's last tick found the CPU"""
        with adfu.Adfu() as link:
            total, size = devblock.ring_size(link.read(devblock.LOG, 12))
            ring = link.read(devblock.LOG + devblock.LOG_HEADER, size)
            last = devblock.crumb(link.read(devblock.CRUMB, devblock.CRUMB_SIZE))
        text = devblock.log_text(total, ring)
        if last is None or devblock.RETURN_LINE in text:
            return text
        pc, lr = Symbolizer(elf or self.image(), self.ws.tool("addr2line")).names(
            [last.pc, last.lr & ~1])
        return text + (f"last tick {last.tick}{' (in a handler)' if last.in_handler else ''}: "
                       f"pc {last.pc:08x} {pc}, lr {last.lr:08x} {lr}\n")

    def read_framebuffer(self, elf: Path | None) -> Image:
        """Checked against the CRC the image logged at its return: a read
        that differs is noted (the picture may still serve)"""
        elf = elf or self.image()
        raw = self._framebuffer_bytes(Elf(elf).address(FB_SYMBOL),
                                      2 * self.lcd.width * self.lcd.height)
        logged = FB_CRC.findall(self.read_log(elf))
        if logged and int(logged[-1], 16) != fb.crc32_mpeg2(raw):
            _note(f"{self.name}: the framebuffer read differs from the image's at its "
                  "return (its logged CRC)")
        return fb.decode(raw, self.lcd)

    def _framebuffer_bytes(self, addr: int, size: int) -> bytes:
        """Plain ROM reads of SRAM; a gadget run (the charger guard, `nor`)
        overwrites the buffer's start, which after_run puts back"""
        with adfu.Adfu() as link:
            return link.read(addr, size)

    def after_run(self, req: RunRequest, returned: bool) -> None:
        if returned:
            with gadget.connect(self.ws, keep=FB_KEEP) as s:
                _note(power.guard(s).describe())

    def battery(self) -> Battery:
        with gadget.connect(self.ws) as s:
            st = power.guard(s)
        return Battery(st.millivolts, st.charging, st.note)

    def shell(self) -> Shell:
        return Shell(self.name, SHELL_VID, self.usb_product or "",
                     lock_file=self.conf.string("shell_lock", "") or None)

    def _card_uuid(self) -> str | None:
        uuid = self.conf.get("card_uuid")
        if uuid is not None and not isinstance(uuid, str):
            raise ConfigError(f"[targets.{self.name}] card_uuid: a string is needed")
        return uuid

    @contextmanager
    def mass_storage(self, seconds: int, image: Path | None = None) -> Generator[Path]:
        """Boots IMAGE (default: config `image`) with usb=storage; holds the
        device lock throughout, as the card commands call this directly"""
        uuid = self._card_uuid()
        if card.find(uuid, self.usb_product):
            raise Ts17Error("the card is on this PC already: unmount or unplug it first")
        req = RunRequest(image or self.image(), seconds, settings={"usb": "storage"})
        with self.lock():
            self.before_run(req)
            self.launch(req)
            deadline = self._launched + seconds + self.return_margin
            try:
                yield card.wait(uuid, alive=lambda: not self._back(), product=self.usb_product)
            finally:
                returned = self.wait_return(deadline, lambda: None)
                self.after_run(req, returned)
            if not returned:
                raise Ts17Error(f"no return to download mode within "
                                f"{seconds + self.return_margin:.0f} s")
