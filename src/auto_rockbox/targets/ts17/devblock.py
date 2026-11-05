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

from .adfu import Ts17Error

DEV_BLOCK = 0x01143000
DEV_BLOCK_SIZE = 0x1000
CFG = DEV_BLOCK
CFG_MAGIC = 0x43445354          # "TSDC"
KEYS_MAX = 0xF0                 # keys[]: a script of up to 239 bytes and its NUL
FLAG_USB = 1 << 0               # let Rockbox start its USB stack (dev builds keep it off)
FLAG_TRACE = 1 << 1             # log a thread dump before the return
LOG = DEV_BLOCK + 0x100
LOG_MAGIC = 0x474C5354          # "TSLG"
LOG_HEADER = 0x10               # the ring follows the header
EMPTY_LOG_HEADER = bytes(12)    # written before a launch: the image starts a fresh log
LAUNCH_TOKEN = 0x4E55524C       # "LRUN" in r0: started by the host, not the boot chain
DEFAULT_RETURN_S = 90           # the image's return time when it finds no config
RETURN_LINE = "returning to download mode at tick"

SETTINGS = "trace=0|1 (a thread dump before the return)"


def run_flags(settings: Mapping[str, str], usb: bool) -> int:
    """The config flags for a run's --set SETTINGS"""
    out = FLAG_USB if usb else 0
    for k, v in settings.items():
        if k != "trace":
            raise Ts17Error(f"--set {k}: ts17 has no such run setting; it takes {SETTINGS}")
        if v not in ("0", "1"):
            raise Ts17Error(f"--set trace={v}: 0 or 1")
        if v == "1":
            out |= FLAG_TRACE
    return out


def run_config(return_s: int, flags: int, keys: str) -> bytes:
    raw = keys.encode()
    if len(raw) >= KEYS_MAX:
        raise Ts17Error(f"key script over {KEYS_MAX - 1} bytes")
    return struct.pack("<IIII", CFG_MAGIC, return_s, flags, 0) + raw.ljust(KEYS_MAX, b"\0")


def ring_size(header: bytes) -> tuple[int, int]:
    """(total, size) from a log header; a missing or bad header is an error"""
    magic, total, size = struct.unpack_from("<III", header)
    if magic != LOG_MAGIC or not 0 < size <= DEV_BLOCK_SIZE:
        raise Ts17Error(f"no dev log at {LOG:#010x} (no image ran since the "
                        "header was cleared, or it is not a dev build)")
    return total, size


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
