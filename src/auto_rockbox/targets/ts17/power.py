"""Charger guard and resets from download mode.

Nothing supervises charging in download mode: the ROM only sets the current,
and stock's last CV level and stop bit carry over. A dev unit sits on USB for
hours, and a Li-ion cell held near 4.2 V ages fastest. So before and after
each run, and in every player command that loads the gadget, the guard lowers
the hardware CV ceiling to its minimum (stock rewrites it at boot) and stops
charging at about 80 %, or when the battery cannot be read.
"""

from __future__ import annotations

from dataclasses import dataclass

from .gadget import Ops, Session

WDT_CTL = 0x4000C020
WDT_RESET_NOW = 0x5F            # enabled, shortest period: stock's reboot path
BOOT_MODE = 0x4000C03C          # retained across resets; read and cleared by mbrec
BOOT_MODE_ADFU = 0x42520100     # "BR" + type 0x0100: enter download mode
FOREVER = 0xFFFFFFFF            # DELAY spins: wait for the reset

CHG_CTL = 0x40004100            # [3:0] current level, [9:5] CV level, bit 19 stop
CHG_CURRENT_MASK = 0xF
CHG_CV_SHIFT = 5
CHG_CV_MASK = 0x1F << CHG_CV_SHIFT
CHG_STOP = 1 << 19
ADC_PEND = 0x40004208           # bit 1: a battery sample is ready (write 1 to clear)
ADC_BAT_READY = 1 << 1
BAT_ADC = 0x40004214            # [13:0] battery sample, 300 mV per 1024
PMU_SETTLE = 3000               # DELAY spins, ~300 us: CHG_CTL writes land after 10-115 us

CHARGE_STOP_MV = 4000           # ~80 %: kinder to the cell than a full charge
CV_GUARD_LEVEL = 0              # the lowest CV, ~4.14 V: a backstop between checks
POLL_TIMEOUT = 0xFFFFFFFF       # the gadget's POLL result when it gave up


def cv_mv(level: int) -> int:
    return (41375 + 125 * level) // 10


@dataclass(frozen=True)
class ChargerState:
    millivolts: int
    fresh: bool                 # a new ADC sample; else the ADC is not running
    charging: bool              # after the guard
    stopped_now: bool           # the guard stopped it
    cv_level: int
    current_level: int

    @property
    def note(self) -> str:
        return (f"CV level {self.cv_level} (~{cv_mv(self.cv_level)} mV), current level "
                f"{self.current_level}" + ("; stopped by the guard" if self.stopped_now else "")
                + ("" if self.fresh else "; stale reading (ADC idle)"))

    def describe(self) -> str:
        return (f"charger {'on' if self.charging else 'off'}, battery ~{self.millivolts} mV; "
                f"{self.note}")


def guard(s: Session) -> ChargerState:
    """Lowers the CV ceiling, reads the battery (eFuse offset of up to
    +-75 mV not applied) and stops charging at >= CHARGE_STOP_MV or when the
    reading is stale"""
    ops = Ops().rmw(CHG_CTL, CHG_CV_MASK, CV_GUARD_LEVEL << CHG_CV_SHIFT)
    ops.delay(PMU_SETTLE).r32(CHG_CTL)
    ops.w32(ADC_PEND, ADC_BAT_READY).poll(ADC_PEND, ADC_BAT_READY, ADC_BAT_READY).r32(BAT_ADC)
    ctl, spins, bat = s.values(ops)
    fresh = spins != POLL_TIMEOUT
    mv = (bat & 0x3FFF) * 300 // 1024
    charging = not ctl & CHG_STOP
    stop = charging and (mv >= CHARGE_STOP_MV or not fresh)
    if stop:
        s.values(Ops().rmw(CHG_CTL, 0, CHG_STOP))
    return ChargerState(mv, fresh, charging and not stop, stop,
                        (ctl & CHG_CV_MASK) >> CHG_CV_SHIFT, ctl & CHG_CURRENT_MASK)


def reset_ops(download: bool) -> Ops:
    """A watchdog reset into download mode, or (not DOWNLOAD) through the
    normal boot chain. Both writes go in one gadget call, so the hook cannot
    set the boot-mode magic again in between."""
    ops = Ops().w32(BOOT_MODE, BOOT_MODE_ADFU if download else 0)
    return ops.w32(WDT_CTL, WDT_RESET_NOW).delay(FOREVER)
