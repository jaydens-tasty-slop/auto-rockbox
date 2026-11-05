"""What each peer is asked to do: its options, with the defaults the
commands show, and the timed steps of a sink run. No Bumble here, so the
commands can build these without the extra."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import UsageError
from .hidkeys import SimButtons

HCI = "127.0.0.1:6402"                  # where the simulator looks for its controller
STACK_ADDRESS = "F0:F0:F0:F0:F0:01"     # virtual controller A (the stack under test)
SINK_ADDRESS = "F0:F0:F0:F0:F0:02"
HID_ADDRESS = "F0:F0:F0:F0:F0:03"

# AVRCP passthrough keys of a sink run's --keys
AVRCP_KEYS = ("play", "pause", "stop", "next", "previous", "volup", "voldown", "ffwd",
              "rewind")
_VALUED = {"vol": 127, "batt": 100, "rate": None, "nodiscover": None, "noopen": None}


def split_hci(spec: str) -> tuple[str | None, int]:
    """HOST:PORT as the virtual controller's server listens on it (HOST "_":
    every interface)"""
    host, _, port = spec.rpartition(":")
    if not host or not port.isdigit():
        raise UsageError(f"{spec!r}: HOST:PORT for the HCI server")
    return (None if host == "_" else host), int(port)


@dataclass(frozen=True)
class StackSettings:
    """Controller A, the one the stack under test drives"""

    address: str = STACK_ADDRESS
    acl_size: int = 1021            # ACL buffer size (Read_Buffer_Size)
    acl_count: int = 8              # ACL buffers
    acl_rate: int = 1000            # air kbit/s that frees them; 0: at once
    eir: bool = True                # False: refuses Write_Inquiry_Mode(2)


@dataclass(frozen=True)
class Step:
    """A timed action in a sink run, T s after the stream first starts:
    an AVRCP key press, or (with a value) vol=, batt=, rate=, nodiscover=,
    noopen=, or lose"""

    t: float
    action: str
    value: int | None = None

    def __str__(self) -> str:
        return self.action if self.value is None else f"{self.action}={self.value}"


def parse_steps(text: str) -> list[Step]:
    """ "5:pause 7:play 9:vol=40 12:lose" (spaces or commas between)"""
    steps = []
    for token in re.split(r"[\s,]+", text.strip()):
        if not token:
            continue
        t, _, what = token.lower().partition(":")
        try:
            at = float(t)
        except ValueError:
            raise UsageError(f"{token!r}: T:KEY, T seconds after the stream starts") from None
        name, eq, value = what.partition("=")
        if eq and name in _VALUED and value.isdigit():
            top = _VALUED[name]
            if top is not None and int(value) > top:
                raise UsageError(f"{token!r}: {name} is at most {top}")
            steps.append(Step(at, name, int(value)))
        elif not eq and (what == "lose" or what in AVRCP_KEYS):
            steps.append(Step(at, what))
        else:
            raise UsageError(f"unknown key {what!r} (use {', '.join(AVRCP_KEYS)}, vol=N, "
                             "batt=N, rate=KBIT, nodiscover=N, noopen=N, lose)")
    return steps


@dataclass
class SinkOptions:
    """bt-peer sink: the virtual A2DP sink + AVRCP controller (+ HFP unit)"""

    out: Path
    hci: str = HCI
    stack: StackSettings = field(default_factory=StackSettings)
    sink_address: str = SINK_ADDRESS
    mtu: int = 672                  # the sink's L2CAP MTU for AVDTP channels
    connect: bool = False           # the sink pages the stack
    connect_delay: float = 3.0
    connect_timeout: float = 60.0
    sink_central: bool = False      # accept the stack's page as central (BlueZ does)
    sink_no_switch: bool = False    # page the stack without allowing a role switch
    configure: bool = False         # with connect: the sink configures and opens
    keys: list[Step] = field(default_factory=list)
    abs_volume: int | None = None   # AVRCP absolute volume, starting here (0..127)
    suspend: float | None = None    # SUSPEND this long after the stream starts
    disconnect: float | None = None
    hf: int | None = None           # an HFP hands-free unit too, battery at this %
    hf_apple: bool = False
    hf_connect: bool = False
    drop_discover: int = 0
    drop_avdtp_open: int = 0
    drop_avrcp: int = 0
    avrcp_query: bool = False
    seconds: float = 60.0           # audio to receive before the report
    expect_freq: float = 1000.0
    exit_after: bool = True
    sdp: bool = False
    allow_unencrypted: bool = False
    forget: bool = False
    verbose: bool = False
    trace: bool = False


@dataclass
class HidOptions:
    """bt-peer hid: a DualShock 4 or keyboard against the stack, virtual
    (against the simulator) or over the air (this PC's adapter)"""

    out: Path
    kind: str = "ds4"
    press: list[str] = field(default_factory=list)
    connect: bool = False           # the device pages the stack, as a paired one does
    connect_delay: float = 5.0
    step: float = 1.0
    hold: float = 0.15              # under the 0.3 s key repeat: one menu step a press
    rate: float | None = None       # DS4 reports/s until told (None: 1000; keyboard 0)
    refuse_boot: bool = False
    legacy: bool = False
    type_delay: float = 0.0
    just_works: bool = False
    silent: bool = False
    unplug: bool = False
    timeout: float = 40.0           # for the HID channels to come up
    forget: bool = False
    verbose: bool = False
    trace: bool = False
    # virtual
    hci: str = HCI
    stack: StackSettings = field(default_factory=StackSettings)
    device_address: str = HID_ADDRESS
    sim: Path | None = None         # a simulator build directory to start
    shots: bool = False
    buttons: SimButtons = field(default_factory=SimButtons)
    # over the air
    stay: float = 0.0
    play: str | None = None

    @property
    def reports_per_s(self) -> float:
        if self.rate is not None:
            return self.rate
        return 1000.0 if self.kind == "ds4" else 0.0


@dataclass
class HeadsetOptions:
    """bt-peer pcheadset: this PC's adapter as a headset paging the player"""

    out: Path
    adapter: str                    # Bumble transport, e.g. usb:VID:PID
    player: str                     # its BD_ADDR
    name: str = "Test Headset"
    cycles: int = 1
    switch_after: tuple[float, float] = (3.0, 3.0)
    switch_share: float = 1.0
    deadline: float = 45.0
    min_packets: int = 20
    play: float = 5.0
    gap: float = 5.0
    repage_wait: float = 25.0
    lose_share: float = 0.0
    relose_share: float = 0.0
    seed: int | None = None


@dataclass
class PcSinkOptions:
    """bt-peer pcsink: this PC as the player's A2DP sink through BlueZ"""

    out: Path
    seconds: int = 30
    wait: int = 600
    name: str = "Rockbox"           # the player's name starts with this
    agent: str = "NoInputNoOutput"
    capture_sink: str = "arb_bt_capture"
