"""Bluetooth test peers for Rockbox's own Bluetooth host stack (A2DP source,
AVRCP, HFP audio gateway, HID host), in the simulator or on a player.

Against the simulator, two Bumble virtual controllers share one LocalLink
(controller.py): controller A is served as H4 over TCP to the simulator,
which uses it as its HCI transport ($ROCKBOX_BT_HCI), and controller B is
the peer, run by Bumble's own host: an A2DP sink with an AVRCP controller
(sink.py) or a DualShock 4 or keyboard in the HID device role (hid.py). The
virtual controller is patched to behave like a real one and reports what a
real controller would refuse. Over the air, this PC's adapter is the peer:
an A2DP sink through BlueZ and PipeWire (pcsink.py), or, driven by Bumble
over raw USB, a headset that pages the player cycle after cycle
(pcheadset.py) or an input device (hid.py).

The Bumble modules need the optional dependency (`uv sync --extra bt`);
options.py, hidkeys.py, sbc.py, media.py, analysis.py and pcsink.py do not.
"""

from __future__ import annotations

import importlib.util

from ..errors import ArbError


class BumbleMissing(ArbError):
    pass


def have_bumble() -> bool:
    return importlib.util.find_spec("bumble") is not None


def need_bumble() -> None:
    """Raises BumbleMissing unless Bumble is installed"""
    if not have_bumble():
        raise BumbleMissing("the Bluetooth peers need Bumble: uv sync --extra bt")
