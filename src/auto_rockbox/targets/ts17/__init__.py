"""The TS17: an Actions "Lark" player (ATJ2157 family, Cortex-M4F), 4 MiB SPI
NOR run in place through an XIP cache, reached through the boot ROM's ADFU
download mode (USB 10d6:10d6).

The dev loop writes the image over stock's application partition (NOR
0x14000, changed sectors only), writes the run config into an SRAM block
that download mode leaves alone, and starts the image in place from a gadget
(gadget.py). The image returns by a watchdog reset with the boot-mode magic
set, after the run time or a hang, and the log ring is read from the same
SRAM block (devblock.py).
"""

from __future__ import annotations

import struct
import sys
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ... import card, fb
from ...elf import Elf, Symbolizer
from ...registry import register_target
from ...target import Battery, Lcd, RunRequest, Target
from ...workspace import ConfigError, Workspace
from . import adfu, commands, devblock, gadget, nor, power, rdm, soak
from .adfu import Ts17Error

if TYPE_CHECKING:
    from PIL.Image import Image

__all__ = ["Ts17", "Ts17Error", "commands", "soak"]

FB_SYMBOL = "lcd_static_framebuffer"    # Rockbox's framebuffer, in SRAM at the gadget's output
FB_KEEP = 0x100                 # the gadget's status block and a few results: what the
                                # charger guard overwrites at the framebuffer's start
SETTLE_S = 0.5                  # download mode answers a moment after it enumerates


def _note(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@register_target
class Ts17(Target):
    """Config ([targets.ts17] in auto-rockbox.toml):
        image        the dev build's ELF that `fb` (without one) and the card
                     commands boot (default: <paths.build>/ts17/rockbox.elf)
        card_uuid    the card's filesystem UUID (default: any Rockbox USB disk)
        stock_cells  a `nor dump --raw` of this unit's whole flash, made before
                     the first run: `nor restore` without an image puts stock's
                     fw0_sys back from it (default: none)

        lock         the device lock file in the temp dir (default:
                     auto-rockbox-ts17.lock); "ts17dev.lock" makes arb and
                     ts17dev exclude each other

    Run settings (--set): trace=1 logs a thread dump before the return;
    volume=DB sets the volume at boot (and so keeps it from step to step).
    """

    name = "ts17"
    summary = "Actions Lark (Cortex-M4F), 4 MiB SPI NOR run in place, ADFU download mode"
    lcd = Lcd(128, 160)
    key_letters = "UDLRSVvP"     # ring, centre, volume up/down, on/off (dev-ts17.c)
    key_suffixes = "+"
    keys_max = devblock.KEYS_MAX - 1
    capabilities = frozenset({"framebuffer", "battery", "cold", "usb", "storage"})
    return_line = devblock.RETURN_LINE
    log_faults = r"lcd: frame timed out|xip codec: bad header"

    def __init__(self, ws: Workspace) -> None:
        super().__init__(ws)
        self._before: tuple[str, str] | None = None   # adfu.present() at the launch
        self._launched = 0.0
        self._cold = False

    def image(self) -> Path:
        return self.conf.opt_path("image") or self.ws.build_dir / "ts17" / "rockbox.elf"

    def _flat(self, elf: Path) -> bytes:
        out = self.ws.build_dir / "arb" / "ts17" / "image.bin"
        out.parent.mkdir(parents=True, exist_ok=True)
        gadget.run_tool([self.ws.tool("objcopy"), "-O", "binary", str(elf), str(out)])
        return out.read_bytes()

    def before_run(self, req: RunRequest) -> None:
        devblock.run_options(req.settings, req.usb)  # bad settings fail before any device call
        with gadget.connect(self.ws) as s:
            _note(power.guard(s).describe())

    def launch(self, req: RunRequest) -> None:
        config = devblock.run_config(req.seconds, devblock.run_options(req.settings, req.usb),
                                     req.keys)
        data = self._flat(req.image)
        sp, reset = struct.unpack_from("<II", data)
        if not nor.XIP_BASE <= reset - 1 < nor.XIP_BASE + len(data):
            raise Ts17Error(f"{req.image.name}: reset vector {reset:08x} is not inside the "
                            f"image at {nor.XIP_BASE:08x}")
        cells = nor.pad(rdm.encode(nor.pad(data, rdm.XIP_RUN)), nor.SECTOR)
        if len(cells) > nor.FW0_SYS_SIZE:
            raise Ts17Error(f"{req.image.name}: {len(cells):#x} bytes do not fit fw0_sys "
                            f"({nor.FW0_SYS_SIZE:#x})")
        with gadget.connect(self.ws) as s:
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
        """Plain ROM reads; a gadget run (the charger guard, `nor`) overwrites
        the buffer's start, which after_run puts back"""
        addr = Elf(elf or self.image()).address(FB_SYMBOL)
        with adfu.Adfu() as link:
            raw = link.read(addr, 2 * self.lcd.width * self.lcd.height)
        return fb.decode(raw, self.lcd)

    def after_run(self, req: RunRequest, returned: bool) -> None:
        if returned:
            with gadget.connect(self.ws, keep=FB_KEEP) as s:
                _note(power.guard(s).describe())

    def battery(self) -> Battery:
        with gadget.connect(self.ws) as s:
            st = power.guard(s)
        return Battery(st.millivolts, st.charging, st.note)

    def _card_uuid(self) -> str | None:
        uuid = self.conf.get("card_uuid")
        if uuid is not None and not isinstance(uuid, str):
            raise ConfigError("[targets.ts17] card_uuid: a string is needed")
        return uuid

    @contextmanager
    def mass_storage(self, seconds: int, image: Path | None = None) -> Generator[Path]:
        """Boots IMAGE (default: config `image`) with USB on; holds the device
        lock throughout, as the card commands call this directly"""
        uuid = self._card_uuid()
        if card.find(uuid):
            raise Ts17Error("the card is on this PC already: unmount or unplug it first")
        req = RunRequest(image or self.image(), seconds, usb=True)
        with self.lock():
            self.before_run(req)
            self.launch(req)
            deadline = self._launched + seconds + self.return_margin
            try:
                yield card.wait(uuid, alive=lambda: not self._back())
            finally:
                returned = self.wait_return(deadline, lambda: None)
                self.after_run(req, returned)
            if not returned:
                raise Ts17Error(f"no return to download mode within "
                                f"{seconds + self.return_margin:.0f} s")
