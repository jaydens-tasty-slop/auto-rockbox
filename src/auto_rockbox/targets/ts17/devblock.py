"""The dev block: run config and log ring in the last 4 KiB of the 16 KiB SRAM
block at 0x01140000, which a reset into download mode leaves alone. The
layout is shared with the dev build (firmware/target/arm/lark/dev-lark.c).

    +0x000  run config, written by the host before a run, consumed once:
            magic "TSDC", then "key=value" lines and a NUL (devlog.h,
            apps/dev_shell.c)
    +0x200  log header, written by the image: magic "TSLG", total bytes
            ever written, ring size, 0; the ring follows
    end-20  the last tick's crumb (Crumb)
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
LOG = DEV_BLOCK + 0x200
CFG_TEXT = LOG - CFG - 4        # the config's lines and their NUL
VOLUME_DB = (-72, 0)            # the setting's range (lark_codec.h)
LOG_MAGIC = 0x474C5354          # "TSLG"
LOG_HEADER = 0x10               # the ring follows the header
EMPTY_LOG_HEADER = bytes(12)    # written before a launch: the image starts a fresh log
CRUMB_MAGIC = 0x43545354        # "TSTC"
CRUMB_SIZE = 20                 # magic, tick, pc, lr, in_handler (dev-lark.c struct dev_crumb)
CRUMB = DEV_BLOCK + DEV_BLOCK_SIZE - CRUMB_SIZE   # every tick rewrites it; cleared at launch
RING_BYTES = CRUMB - LOG - LOG_HEADER   # dev-lark.c RING_BYTES
LAUNCH_TOKEN = 0x4E55524C       # "LRUN" in r0: started by the host, not the boot chain
DEFAULT_RETURN_S = 90           # the image's return time when it finds no config
RETURN_LINE = "returning to download mode at tick"

# The run settings the image takes (apps/dev_shell.c, dev-lark.c), as
# accepted values; None takes any
SETTINGS: Mapping[str, tuple[str, ...] | None] = {
    "trace": ("0", "1"),        # a thread dump before the return
    "volume": None,             # dB, set at boot (and so kept from step to step)
    "bluetooth": ("off", "card"),   # off for this boot, or as the card's settings say
    "usb": ("0", "1", "storage"),   # USB with the dev shell (charge only), or mass storage
    "play": None, "ls": None, "monitor": None, "sdlog": ("0", "1"),
    "poweroff": ("real",), "crash": ("udf", "panic"),
    "sdtest": None, "sh": None,     # dev_boot: an SD write/read check, shell commands
    "then": None,                   # shell commands once the key script is done
    "memtest": None,                # dev_boot: KiB of the buffer heap written and read back
}


def check_settings(settings: Mapping[str, str]) -> None:
    """A run's --set SETTINGS, refused before any device call when the image
    would not take them"""
    for k, v in settings.items():
        if k not in SETTINGS:
            raise Ts17Error(f"--set {k}: ts17 has no such run setting; it takes "
                            + ", ".join(sorted(SETTINGS)))
        allowed = SETTINGS[k]
        if allowed is not None and v not in allowed:
            raise Ts17Error(f"--set {k}={v}: one of {'|'.join(allowed)}")
        if k == "volume":
            lo, hi = VOLUME_DB
            if not v.lstrip("-").isdigit() or not lo <= int(v) <= hi:
                raise Ts17Error(f"--set volume={v}: whole dB, {lo}..{hi}")


def run_config(lines: Mapping[str, str]) -> bytes:
    """The config block for LINES (Target.run_config)"""
    text = "".join(f"{k}={v}\n" for k, v in lines.items()).encode()
    if len(text) >= CFG_TEXT:
        raise Ts17Error(f"run config over {CFG_TEXT - 1} bytes (a long key script?)")
    return struct.pack("<I", CFG_MAGIC) + text.ljust(CFG_TEXT, b"\0")


def ring_size(header: bytes) -> tuple[int, int]:
    """(total, size) from a log header; a missing or bad header is an error"""
    magic, total, size = struct.unpack_from("<III", header)
    if magic != LOG_MAGIC or size != RING_BYTES:
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
