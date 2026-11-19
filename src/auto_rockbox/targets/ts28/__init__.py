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
from ..ts17 import Ts17

__all__ = ["Ts28"]


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
    capabilities = frozenset({"battery", "storage"})
    dev_dir = "/ts28test"
    model_tag: ClassVar[str] = "xlx_TS28"   # NOR 0x1338 (Lark fact L-F-0018)
    usb_product: ClassVar[str | None] = "0003"     # ts28.h
