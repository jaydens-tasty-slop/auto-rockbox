"""The HID peer's keys: the reports a DualShock 4 and a boot keyboard send
for them, the DS4's output report, and what the stack under test should
make of each press (the buttons the simulator holds, the key bits the
stack's HID driver logs)."""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

KINDS = {                               # name and class of device
    "ds4": ("Wireless Controller", 0x002508),
    "keyboard": ("BT Keyboard", 0x002540),
}
DIRECTIONS = ("up", "down", "left", "right")
STICK = {"sup": "up", "sdown": "down", "sleft": "left", "sright": "right"}  # the left stick
PAD = ("a", "b", "x", "y", "l1", "r1", "l2", "r2", "menu", "view", "home")
PAD_SHIFT = 13                          # button.h's BUTTON_PAD_SHIFT unless a target moves it

# DualShock 4 report 0x01: LX LY RX RY B0 B1 B2 L2 R2; B0's low nibble is
# the hat (8 = centred), B2's top six bits a report counter
DS4_HAT = {"up": 0, "right": 2, "down": 4, "left": 6}
DS4_BITS = {"a": (4, 0x20), "b": (4, 0x40), "x": (4, 0x10), "y": (4, 0x80),
            "l1": (5, 0x01), "r1": (5, 0x02), "l2": (5, 0x04), "r2": (5, 0x08),
            "view": (5, 0x10), "menu": (5, 0x20), "home": (6, 0x01)}
DS4_STICK = {"sup": (1, 0), "sdown": (1, 255), "sleft": (0, 0), "sright": (0, 255)}
DS4_EXTENDED_LEN = 78                   # report 0x11, once the host asked for it
DS4_OUTPUT_LEN = 79                     # 0xA2 and output report 0x11, CRC-32 last
# Keyboard usages (HID Usage Tables, keyboard page)
KBD_USAGE = {"up": 0x52, "down": 0x51, "left": 0x50, "right": 0x4F, "a": 0x28,
             "b": 0x29, "l1": 0x4B, "r1": 0x4E, "menu": 0x65, "view": 0x2B,
             "home": 0x4A}


def keys_for(kind: str) -> tuple[str, ...]:
    """The keys device KIND has"""
    if kind == "keyboard":
        return tuple(KBD_USAGE)
    return (*DIRECTIONS, *STICK, *PAD)


def _direction(key: str) -> str | None:
    return key if key in DIRECTIONS else STICK.get(key)


def stack_bits(key: str) -> int:
    """KEY's bit in the stack's HID key state (hid.c's K_*), as its
    "bt: hid keys" lines show it"""
    d = _direction(key)
    return 1 << DIRECTIONS.index(d) if d else 0x10 << PAD.index(key)


@dataclass(frozen=True)
class SimButtons:
    """The simulator target's buttons that the keys hold: its button-target.h
    BUTTON_UP/DOWN/LEFT/RIGHT and BUTTON_PAD_SHIFT"""

    up: int = 0x01
    down: int = 0x02
    left: int = 0x04
    right: int = 0x08
    pad_shift: int = PAD_SHIFT

    def held(self, key: str) -> int:
        d = _direction(key)
        return getattr(self, d) if d else (1 << PAD.index(key)) << self.pad_shift


def keyboard_report(pressed: Iterable[str]) -> bytes:
    """Boot keyboard input report 1: no modifiers, up to six usages"""
    keys = [KBD_USAGE[k] for k in sorted(pressed) if k in KBD_USAGE][:6]
    return bytes([0x01, 0, 0, *keys, *[0] * (6 - len(keys))])


def ds4_report(pressed: Iterable[str], counter: int, extended: bool) -> bytes:
    """DS4 input report 0x01, or 0x11 (two more bytes first, 78 in all) once
    the host has sent output report 0x11"""
    r = bytearray([0x01, 0x80, 0x80, 0x80, 0x80, 0x08, 0, 0, 0, 0])
    for k in pressed:
        if k in DS4_HAT:
            r[5] = (r[5] & 0xF0) | DS4_HAT[k]
        elif k in DS4_BITS:
            i, bit = DS4_BITS[k]
            r[1 + i] |= bit
        elif k in DS4_STICK:
            i, v = DS4_STICK[k]
            r[1 + i] = v
    r[7] |= (counter & 0x3F) << 2
    if not extended:
        return bytes(r)
    out = bytes([0x11, 0xC0, 0x00]) + r[1:]
    return out + bytes(DS4_EXTENDED_LEN - len(out))


@dataclass(frozen=True)
class Ds4Output:
    """Output report 0x11 from the host: the report interval and light bar"""

    control: int
    flags: int
    light: tuple[int, int, int]

    @property
    def interval_ms(self) -> int:
        return self.control & 0x3F


def ds4_output(pdu: bytes) -> Ds4Output | str | None:
    """PDU (an interrupt channel DATA message) as DS4 output report 0x11: None
    if it is something else, the problem if its length or CRC-32 (over 0xA2
    and the report) is wrong"""
    if pdu[:2] != b"\xa2\x11":
        return None
    if (len(pdu) != DS4_OUTPUT_LEN
            or zlib.crc32(pdu[:75]) != int.from_bytes(pdu[75:], "little")):
        return f"DS4 output report: bad length or CRC: {pdu.hex()}"
    r = pdu[1:]
    return Ds4Output(r[1], r[3], (r[8], r[9], r[10]))


# ---------------------------------------------------------------- what the stack made of it

_SIM_BUTTONS = re.compile(r"bt-test: buttons ([0-9a-f]+)")
_STACK_KEYS = re.compile(r"bt: hid keys ([0-9a-f]+)")
_SIM_NOTES = re.compile(r"bt: hid [a-z].*|bt-test: (?:pairing|volume now|paired).*")
_HID_UP = re.compile(r"bt: hid \w+ connected")


@dataclass(frozen=True)
class Outcome:
    seen: list[int]
    want: list[int]
    notes: list[str]            # the log lines that tell the story

    @property
    def ok(self) -> bool:
        return self.seen == self.want


def check_sim_log(text: str, presses: Sequence[str], buttons: SimButtons) -> Outcome:
    """The buttons the simulator held (its "bt-test: buttons" lines, without
    the releases) against those the presses should hold"""
    seen = [v for v in (int(x, 16) for x in _SIM_BUTTONS.findall(text)) if v]
    keys = " ".join(_STACK_KEYS.findall(text)) or "-"
    notes = [f"sim buttons held: {' '.join(f'{v:02x}' for v in seen) or '-'}",
             f"expected:         {' '.join(f'{buttons.held(k):02x}' for k in presses) or '-'}",
             f"stack key states: {keys}"]
    notes += [f"sim: {m.group(0)}" for m in _SIM_NOTES.finditer(text)]
    return Outcome(seen, [buttons.held(k) for k in presses], notes)


def check_player_log(lines: Sequence[str], presses: Sequence[str]) -> Outcome:
    """The key states the player's log shows since the device last
    connected, without the releases, against the presses"""
    start = max((i for i, ln in enumerate(lines) if _HID_UP.search(ln)), default=0)
    seen = [int(v, 16) for ln in lines[start:] for v in _STACK_KEYS.findall(ln)]
    seen = [v for v in seen if v]
    want = [stack_bits(k) for k in presses]
    return Outcome(seen, want, [
        f"player key states: {' '.join(f'{v:03x}' for v in seen) or '-'}",
        f"expected:          {' '.join(f'{v:03x}' for v in want) or '-'}"])
