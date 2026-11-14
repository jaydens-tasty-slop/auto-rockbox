"""The TS17 rescue stub (rockbox rescue-lark.S), run from a build in the
unicorn emulator: every branch, against modelled PMU, watchdog and SysTick
registers and the key ladder, with no RAM mapped (stack or memory use faults). Needs unicorn
(`uv pip install unicorn`) and a build: TS17_ELF, else build/ts17/rockbox.elf."""

from __future__ import annotations

import importlib
import os
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any

from auto_rockbox.elf import Elf
from auto_rockbox.targets.ts17 import nor, power, rdm

from . import Skip

BUILD_ELF = Path(__file__).resolve().parents[3] / "build" / "ts17" / "rockbox.elf"
DET, DET_DC5V = 0x40004010, 1 << 0          # PMU: USB power
KEY, KEY_ONOFF = 0x40004108, 1 << 17         # PMU: on/off held
ADC_CTL, ADC_LADDER = 0x40004200, 1 << 5 | 1 << 23   # the ladder channel, run
ADC_KEYS = 0x40004224                       # the ladder sample
IDLE, DOWN = 0x3190, 0x211C                 # measured ladder steps
PMU_OFF, PMU_WAKE = 0x4000410C, 0x40004110  # power-off: wake sources, then any write
WAKE_ON_BATTERY = 0x115                     # power-ts17.c's on battery: base | DC5V
SYST_CSR, SYST_RVR, SYST_CVR = 0xE000E010, 0xE000E014, 0xE000E018
WDT_ARM, WDT_NOW = 0x1D, 0x1E               # ~11 s with a feed; reset now
HOLD_PERIODS = 150                          # 150 checks, 10 ms apart, after a first wait


class _Stop(Exception):
    pass


def _flash() -> bytes:
    path = Path(os.environ.get("TS17_ELF", BUILD_ELF))
    if not path.exists():
        raise Skip(f"no {path}")
    secs = Elf(path).sections
    if ".rescue" not in secs:
        raise Skip(f"{path} has no rescue stub")
    r, t = secs[".rescue"], secs[".text"]
    raw = Elf(path).raw
    assert r.addr == nor.XIP_BASE and r.size == nor.SECTOR and t.addr == nor.APP_BASE
    return raw[r.offset:r.offset + r.size] + raw[t.offset:t.offset + 8]


Keys = Callable[[int], tuple[bool, int]]   # periods passed -> (on/off held, ladder sample)


def _run(uc_mod: Any, flash: bytes, usb: bool, keys: Keys,
         app: bytes | None = None, *, held_reads: int = 0) -> tuple[str, int]:
    """Where the stub went ("app", "adfu", "off") and after how many 10 ms
    periods. HELD_READS: on/off reads held for that many reads, whatever
    KEYS says (no periods pass on battery)."""
    arm = importlib.import_module("unicorn.arm_const")
    uc = uc_mod.Uc(uc_mod.UC_ARCH_ARM, uc_mod.UC_MODE_THUMB | uc_mod.UC_MODE_MCLASS)
    mem = bytearray(flash[:nor.SECTOR] + (app or flash[nor.SECTOR:])).ljust(0x200000, b"\0")
    uc.mem_map(nor.XIP_BASE, len(mem))
    uc.mem_write(nor.XIP_BASE, bytes(mem))
    st: dict[str, Any] = {"periods": 0, "csr": 0, "reads": 0, "writes": [], "out": None,
                          "adc": 0x800000, "key_reads": 0, "wake": None}

    def pmu_read(_uc: Any, off: int, _size: int, _d: Any) -> int:
        addr = 0x40004000 + off
        if addr == DET:
            return DET_DC5V if usb else 0
        if addr == KEY:
            st["key_reads"] += 1
            if st["key_reads"] <= held_reads:
                return KEY_ONOFF
            return KEY_ONOFF if keys(st["periods"])[0] else 0
        if addr == ADC_CTL:
            return st["adc"]
        if addr == ADC_KEYS:
            if st["adc"] & ADC_LADDER != ADC_LADDER:
                raise _Stop("ladder read before its channel is on")
            return keys(st["periods"])[1]
        raise _Stop(f"PMU read {addr:08x}")

    def pmu_write(_uc: Any, off: int, _size: int, v: int, _d: Any) -> None:
        addr = 0x40004000 + off
        if addr == ADC_CTL and v == st["adc"] | ADC_LADDER:
            st["adc"] = v
        elif addr == PMU_WAKE and not usb and not keys(st["periods"])[0] \
                and st["key_reads"] > held_reads:
            st["wake"] = v
        elif addr == PMU_OFF and st["wake"] == WAKE_ON_BATTERY:
            st["out"] = "off"
            uc.emu_stop()
        else:
            raise _Stop(f"PMU write {addr:08x}={v:x}")

    def sys_write(_uc: Any, off: int, _size: int, v: int, _d: Any) -> None:
        addr = 0x4000C000 + off
        st["writes"].append((addr, v))
        if addr == power.WDT_CTL and v == WDT_NOW:
            st["out"] = "adfu"
            uc.emu_stop()
        elif addr not in (power.WDT_CTL, power.BOOT_MODE):
            raise _Stop(f"write {addr:08x}")

    def scs_read(_uc: Any, off: int, _size: int, _d: Any) -> int:
        if 0xE000E000 + off != SYST_CSR:
            raise _Stop(f"SCS read {0xE000E000 + off:08x}")
        st["reads"] += 1
        if st["csr"] == 5 and st["reads"] % 3 == 0:    # a period ends every third read
            st["periods"] += 1
            return 5 | 1 << 16
        return st["csr"]

    def scs_write(_uc: Any, off: int, _size: int, v: int, _d: Any) -> None:
        addr = 0xE000E000 + off
        if addr == SYST_CSR:
            st["csr"] = v
        elif (addr, v) not in ((SYST_RVR, 96_000_000 // 100 - 1), (SYST_CVR, 0)):
            raise _Stop(f"SCS write {addr:08x}={v:x}")

    def fail(*_a: Any) -> Any:
        raise _Stop("unexpected access")

    uc.mmio_map(0x40004000, 0x1000, pmu_read, None, pmu_write, None)
    uc.mmio_map(0x4000C000, 0x1000, fail, None, sys_write, None)
    uc.mmio_map(0xE000E000, 0x1000, scs_read, None, scs_write, None)
    target = struct.unpack_from("<I", mem, nor.SECTOR + 4)[0] & ~1

    def code(_uc: Any, addr: int, _size: int, _d: Any) -> None:
        if not nor.XIP_BASE <= addr < nor.APP_BASE:
            st["out"] = "app" if addr == target else f"jumped to {addr:08x}"
            uc.emu_stop()

    uc.hook_add(uc_mod.UC_HOOK_CODE, code)
    uc.reg_write(arm.UC_ARM_REG_R0, 0xA5A5A5A5)
    uc.emu_start(struct.unpack_from("<I", mem, 4)[0], 0, count=20_000_000)
    assert st["writes"][:2] == [(power.BOOT_MODE, power.BOOT_MODE_ADFU),
                                (power.WDT_CTL, WDT_ARM)], st["writes"]
    if st["out"] == "app":
        assert uc.reg_read(arm.UC_ARM_REG_R0) == 0xA5A5A5A5, "r0 not kept"
        assert len(st["writes"]) == 2, st["writes"]
    return str(st["out"]), int(st["periods"])


def test_rescue_stub_branches() -> None:
    try:
        uc_mod = importlib.import_module("unicorn")
    except ImportError as e:
        raise Skip("no unicorn") from e
    flash = _flash()
    assert flash[8:12] == nor.RESCUE_MARK
    assert nor.is_frozen_rescue(flash), "the build's stub is not the frozen one"
    nor.check_image(flash, "build")         # arb would accept the image

    def v(sp: int, reset: int) -> bytes:
        return struct.pack("<II", sp, reset)

    def keys(onoff: int = 0, ladder: int = IDLE, down: int = 0) -> Keys:
        """on/off held for ONOFF periods, Down for DOWN; LADDER otherwise"""
        return lambda n: (n < onoff, DOWN if n < down else ladder)

    forever = 1 << 30
    def alternate(n: int) -> tuple[bool, int]:
        return n % 2 == 0, IDLE if n % 2 == 0 else DOWN

    cases: list[tuple[str, bool, Keys, bytes | None, tuple[str, int]]] = [
        ("no USB, no key", False, keys(), None, ("app", 0)),
        ("USB, no key", True, keys(), None, ("app", 1)),
        ("on/off, no USB", False, keys(onoff=forever), None, ("app", 0)),
        ("Down, no USB", False, keys(down=forever), None, ("app", 0)),
        ("USB + on/off held", True, keys(onoff=forever), None, ("adfu", HOLD_PERIODS)),
        ("USB + Down held", True, keys(down=forever), None, ("adfu", HOLD_PERIODS)),
        ("on/off and Down in turn", True, alternate, None, ("adfu", HOLD_PERIODS)),
        ("on/off let go after 0.5 s", True, keys(onoff=50), None, ("app", 50)),
        ("Down let go at the last check", True, keys(down=HOLD_PERIODS), None,
         ("app", HOLD_PERIODS)),
        ("ladder just below Down", True, keys(ladder=0x1CFF), None, ("app", 1)),
        ("ladder at Down's floor", True, keys(ladder=0x1D00), None, ("adfu", HOLD_PERIODS)),
        ("ladder at Down's top", True, keys(ladder=0x24FF), None, ("adfu", HOLD_PERIODS)),
        ("ladder just above Down", True, keys(ladder=0x2500), None, ("app", 1)),
        ("ladder 0 (Prev, or no sample)", True, keys(ladder=0), None, ("app", 1)),
        ("Down with high bits set", True, keys(ladder=0xFFFFC000 | DOWN), None,
         ("adfu", HOLD_PERIODS)),
        ("reset at the last byte", False, keys(), v(0x010087F0, 0x101A6FFF), ("app", 0)),
        ("SP at RAM0's top", False, keys(), v(0x01024000, 0x10001141), ("app", 0)),
    ]
    for name, usb, k, app, want in cases:
        got = _run(uc_mod, flash, usb, k, app)
        assert got == want, f"{name}: {got}, want {want}"

    # No image to run: download mode with USB power, else power-off (after
    # the on/off key is let go, as a held key would wake it again)
    bad = {
        "erased flash, as read through the randomizer": rdm.decode(b"\xff" * rdm.XIP_RUN)[:8],
        "an ELF file": b"\x7fELF\x01\x01\x01\0",
        "zeros": bytes(8),
        "ones": b"\xff" * 8,
        "old layout": v(0x010087F0, 0x10000141),
        "no Thumb bit": v(0x010087F0, 0x10001140),
        "reset past the image": v(0x010087F0, 0x101A7001),
        "SP at RAM0's base": v(0x01000000, 0x10001141),
        "SP past RAM0": v(0x01024004, 0x10001141),
        "SP in PSRAM": v(0x18001000, 0x10001141),
    }
    for name, app in bad.items():
        assert _run(uc_mod, flash, True, keys(), app) == ("adfu", 1), f"{name}, USB"
        assert _run(uc_mod, flash, False, keys(), app) == ("off", 0), f"{name}, battery"
    assert _run(uc_mod, flash, False, keys(), bytes(8), held_reads=1000) == ("off", 0), \
        "on/off still held from the power-on"
