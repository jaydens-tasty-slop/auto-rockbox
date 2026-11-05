"""The dev block: run config and log ring in the last 4 KiB of the 16 KiB SRAM
block at 0x01140000, which a reset into download mode leaves alone. The
layout is shared with the dev build (firmware/target/arm/lark/ts17/dev-ts17.c).

    +0x000  config, written by the host before a run, consumed once:
            magic "TSDC", return_s (0 = never), flags, 0, keys[0xf0]
    +0x100  log header, written by the image: magic "TSLG", total bytes
            ever written, ring size, 0; the ring follows
"""

from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass

from .adfu import Ts17Error

DEV_BLOCK = 0x01143000
DEV_BLOCK_SIZE = 0x1000
CFG = DEV_BLOCK
CFG_MAGIC = 0x43445354          # "TSDC"
KEYS_MAX = 0xF0                 # keys[]: a script of up to 239 bytes and its NUL
FLAG_USB = 1 << 0               # let Rockbox start its USB stack (dev builds keep it off)
FLAG_TRACE = 1 << 1             # log a thread dump before the return
FLAG_VOLUME = 1 << 2            # set the volume (the config's fourth word, dB) at boot
FLAG_BT_OFF = 1 << 3            # Bluetooth off at boot, whatever the settings say
VOLUME_DB = (-72, 0)            # the setting's range (lark_codec.h)
LOG = DEV_BLOCK + 0x100
LOG_MAGIC = 0x474C5354          # "TSLG"
LOG_HEADER = 0x10               # the ring follows the header
EMPTY_LOG_HEADER = bytes(12)    # written before a launch: the image starts a fresh log
CRUMB_MAGIC = 0x43545354        # "TSTC"
CRUMB_SIZE = 20                 # magic, tick, pc, lr, in_handler (dev-ts17.c struct dev_crumb)
CRUMB = DEV_BLOCK + DEV_BLOCK_SIZE - CRUMB_SIZE   # every tick rewrites it; cleared at launch
LAUNCH_TOKEN = 0x4E55524C       # "LRUN" in r0: started by the host, not the boot chain
DEFAULT_RETURN_S = 90           # the image's return time when it finds no config
RETURN_LINE = "returning to download mode at tick"

SETTINGS = ("trace=0|1 (a thread dump before the return), volume=DB (-72..0, set once the "
            "settings are loaded and kept), bluetooth=off|card (off for this boot, or as the "
            "card's settings say)")


@dataclass(frozen=True)
class RunOptions:
    flags: int = 0
    volume: int = 0             # dB, with FLAG_VOLUME


def run_options(settings: Mapping[str, str], usb: bool) -> RunOptions:
    """The config's flags and volume for a run's --set SETTINGS"""
    flags, volume = FLAG_USB if usb else 0, 0
    for k, v in settings.items():
        if k == "trace":
            if v not in ("0", "1"):
                raise Ts17Error(f"--set trace={v}: 0 or 1")
            flags |= FLAG_TRACE if v == "1" else 0
        elif k == "volume":
            lo, hi = VOLUME_DB
            if not v.lstrip("-").isdigit() or not lo <= int(v) <= hi:
                raise Ts17Error(f"--set volume={v}: whole dB, {lo}..{hi}")
            flags, volume = flags | FLAG_VOLUME, int(v)
        elif k == "bluetooth":
            if v not in ("off", "card"):
                raise Ts17Error(f"--set bluetooth={v}: off or card")
            flags |= FLAG_BT_OFF if v == "off" else 0
        else:
            raise Ts17Error(f"--set {k}: ts17 has no such run setting; it takes {SETTINGS}")
    return RunOptions(flags, volume)


def run_config(return_s: int, opts: RunOptions, keys: str) -> bytes:
    raw = keys.encode()
    if len(raw) >= KEYS_MAX:
        raise Ts17Error(f"key script over {KEYS_MAX - 1} bytes")
    head = struct.pack("<IIIi", CFG_MAGIC, return_s, opts.flags, opts.volume)
    return head + raw.ljust(KEYS_MAX, b"\0")


def ring_size(header: bytes) -> tuple[int, int]:
    """(total, size) from a log header; a missing or bad header is an error"""
    magic, total, size = struct.unpack_from("<III", header)
    if magic != LOG_MAGIC or not 0 < size <= DEV_BLOCK_SIZE:
        raise Ts17Error(f"no dev log at {LOG:#010x} (no image ran since the "
                        "header was cleared, or it is not a dev build)")
    return total, size


@dataclass(frozen=True)
class Crumb:
    """Where the image's last tick found the CPU"""

    tick: int
    pc: int
    lr: int
    in_handler: bool            # the tick preempted another handler


def crumb(raw: bytes) -> Crumb | None:
    """The last tick's record, or None (no image with one ran since launch)"""
    magic, tick, pc, lr, handler = struct.unpack("<IIIII", raw)
    return Crumb(tick, pc, lr, bool(handler)) if magic == CRUMB_MAGIC else None


def log_text(total: int, ring: bytes) -> str:
    """The ring unwrapped, oldest first, and a last line saying how much was
    logged and whether older lines are lost"""
    size = len(ring)
    if total <= size:
        text = ring[:total]
    else:
        start = total % size
        text = ring[start:] + ring[:start]
    s = text.decode("latin-1")
    if s and not s.endswith("\n"):
        s += "\n"
    return s + f"-- {total} bytes logged{' (ring wrapped)' if total > size else ''}\n"
