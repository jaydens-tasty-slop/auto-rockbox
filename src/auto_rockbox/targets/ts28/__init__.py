"""The TS28: a Tengsen player on the Actions "Lark" SoC, like the TS17
(targets/ts17), with a 240x320 touch panel, a speaker and six keys.

The boot ROM, the boot chain (byte-identical to the TS17's), the flash layout
(stock's application at NOR 0x14000, which our image replaces behind the
same frozen rescue stub) and the dev loop's transport are the TS17's, so this
is the TS17's target with the TS28's board: its model text (checked before
every write: both players are 10d6:10d6 in download mode), panel, keys and
dev build. Bring-up state: docs/ts28/ in the port's tree.
"""

from __future__ import annotations

from typing import ClassVar

from ...registry import register_target
from ...target import Lcd
from ..ts17 import Ts17, gadget
from ..ts17.adfu import Ts17Error

__all__ = ["Ts28"]

# PSRAM after a return (docs/ts28/hardware.md, Rockbox on the TS28): the
# boot chain sets its controller up but leaves XIP mapping entry 1 out (reads
# give junk), and its cache may answer with stale lines even through the
# uncached view. Entry 1 as at application entry (Lark fact L-F-0019):
PSRAM_MAP = ((0x40010308, 0x1800000F), (0x4001030C, 0))
PSRAM_CACHE_OP = 0x40018004     # lark.h LARK_PSRAM_CACHE_OP: discard is INV | GO
PSRAM_CACHE_DISCARD, PSRAM_CACHE_GO = 0x9, 0x1
PSRAM_BASE, PSRAM_UNCACHED, PSRAM_SIZE = 0x18000000, 0x1C000000, 0x400000
BOUNCE = 0x01041000             # SRAM past the gadget's results (gadget.GADGET_OUT)
BOUNCE_BYTES = 0x12C00          # up to 0x01053c00, in the 0x01040000..0x01058000 block


@register_target
class Ts28(Ts17):
    """Config ([targets.ts28] in auto-rockbox.toml): the TS17's keys (image,
    card_uuid, stock_cells, model_tag, lock, shell_lock, bt_address), for
    this player.

    Keys for key scripts: B back, S play/pause (select), M the "M" key,
    V v volume up and down (skip right and left), P power.
    """

    name = "ts28"
    summary = ("Actions Lark (Cortex-M4F), 240x320 touch panel, 4 MiB SPI NOR run in place, "
               "ADFU download mode")
    lcd = Lcd(240, 320)
    key_letters = "BSMVvP"
    # Grows as the drivers come up (docs/ts28/checklist.md); the battery
    # reading and the charger guard are the chip's, as on the TS17
    capabilities = frozenset({"battery", "storage", "framebuffer"})
    dev_dir = "/ts28test"
    model_tag: ClassVar[str] = "xlx_TS28"   # NOR 0x1338 (Lark fact L-F-0018)
    usb_product: ClassVar[str | None] = "0003"     # ts28.h

    def _framebuffer_bytes(self, addr: int, size: int) -> bytes:
        """The framebuffer is in PSRAM: the gadget maps it again, discards the
        cache and copies it through the uncached view into SRAM, which the ROM
        reads. The return itself flips a few bits (the same cells each time:
        ~80 single-pixel specks a frame), so the logged CRC rarely matches."""
        if PSRAM_BASE <= addr < PSRAM_BASE + PSRAM_SIZE:
            addr += PSRAM_UNCACHED - PSRAM_BASE
        out = bytearray()
        with gadget.connect(self.ws) as s:
            ops = gadget.Ops()
            for reg, val in PSRAM_MAP:
                ops.w32(reg, val)
            s.values(ops.w32(PSRAM_CACHE_OP, PSRAM_CACHE_DISCARD)
                     .poll(PSRAM_CACHE_OP, PSRAM_CACHE_GO, 0))
            for off in range(0, size, BOUNCE_BYTES):
                n = min(BOUNCE_BYTES, size - off)
                r = s.run(gadget.Ops().copy(addr + off, BOUNCE, n))
                if r.nfault:
                    raise Ts17Error(f"framebuffer copy: {r.describe_faults()}")
                out += s.link.read(BOUNCE, n)
        return bytes(out)
