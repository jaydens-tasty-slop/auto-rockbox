"""The TS17's torture run: the shared Rockbox torture (suites/rockbox_torture.py)
in the TS17's dev folder.

    arb ts17 torture build/torture-ts17/rockbox.elf build/torture/RUN --hours 6

The TS17's build has no shell commands of its own, so it runs the shared
scenarios but those that need what only the YP3 has: the LCD clock gate read
(sleep) and tuning without the radio screen (its recordings use the
microphone). The settings files go into /ts17test/ once per run, over
USB mass storage. A crash or hang leaves the dev log only (the TS17 keeps
no fault record or thread snapshot).
"""

from __future__ import annotations

from ...suites.rockbox_torture import BT_PC, RockboxTorture
from ...suites.torture import player_suite


@player_suite("ts17")
class Ts17Torture(RockboxTorture):
    """The torture run on the TS17. Config: as RockboxTorture's ([targets.ts17]
    bt_address, menu_coverage)."""

    def boot_settings(self) -> dict[str, str]:
        # The TS17 connects to this PC at boot when its Bluetooth is on, and
        # its stream would leave Line In for this PC's speakers. Off then,
        # unless this PC can be its headset (BT_PC, [targets.ts17] bt_address):
        # then the bt-* scenarios need the radio on, the run keeps the stream
        # on a null sink, and `linein` moves it to the built-in output for its
        # tone (as on the YP3). Keyed to the config, not to --only: a run
        # without bt-* scenarios but with the address boots with the card's
        # setting.
        return {} if BT_PC in self.features else {"bluetooth": "off"}
