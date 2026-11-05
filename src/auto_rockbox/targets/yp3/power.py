"""The cell and the charger from ROM USB, through the payload's PMU access.

Nothing in ROM USB stops the charger: left on for hours it went far past
stock's full point. So a run switches it off at a limit before it starts and
for good after the return; Rockbox runs its own charging while it runs. The
charge log charges to full from ROM USB the way stock firmware does (charger
on, off at the PMU's charge-full event) and logs a CSV line a minute.
"""

from __future__ import annotations

import csv
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ...lock import DeviceBusy
from ...target import Battery
from .rom import DeviceError, Rom, wait_rom

PMU_CHARGE = 0x17           # bit 2 (CHARGE_ON): the charger on
CHARGE_ON = 0x04
PMU_ADC = 0x47              # bit 4 (ADC_SAMPLE) set: sample the battery into PMU_BATTERY
ADC_SAMPLE = 0x10
PMU_BATTERY = 0x2E          # the battery sample, low 7 bits
PMU_POWER = 0x2A            # bit 2 (EXT_POWER): external (USB) power present
EXT_POWER = 0x04
PMU_EVENTS = 0x30           # bit 1 (CHARGE_FULL): the charge-full event; write back to clear
CHARGE_FULL = 0x02


def millivolts(raw: int) -> int:
    """Stock firmware's conversion; not yet checked against a meter"""
    return int(2800 + 14.173 * (raw & 127))


def read_millivolts(rom: Rom) -> int:
    r47 = rom.pmu_read(PMU_ADC)
    rom.pmu_write(PMU_ADC, r47 | ADC_SAMPLE)
    time.sleep(0.05)
    raw = rom.pmu_read(PMU_BATTERY)
    rom.pmu_write(PMU_ADC, r47 & ~ADC_SAMPLE)
    return millivolts(raw)


def guard(rom: Rom, limit_mv: int, log: Path, stop: bool = False) -> Battery:
    """Reads the cell; the charger goes off at LIMIT_MV, or at any voltage
    with STOP. One line per call in LOG."""
    mv = read_millivolts(rom)
    charge = rom.pmu_read(PMU_CHARGE)
    on = bool(charge & CHARGE_ON)
    msg = f"battery ~{mv} mV, charging {'on' if on else 'off'} (pmu17={charge:#x})"
    note = ""
    if on and (stop or mv >= limit_mv):
        rom.pmu_write(PMU_CHARGE, charge & ~CHARGE_ON)
        on = False
        note = "charging disabled" + ("" if stop else f" (limit {limit_mv} mV)")
        msg += " -> " + note
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as f:
        f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    return Battery(mv, on, note)


# The charge log's pace
PERIOD = 60                 # s between samples
PROBE_EVERY = 600           # s between near-rest readings while charging
PROBE_PAUSE = 60            # s the charger is off before one
REST_AFTER_FULL = 1800      # s of rest readings after full
RETRY_S = 50                # an unreachable player is retried this long per sample
MAX_CHARGE_S = 8 * 3600
SAFETY_MV = 4400            # a reading while charging; the charger's CV tier is 4250 mV

Session = Callable[[], AbstractContextManager[Rom]]


@dataclass
class Sample:
    kind: str
    mv: int
    pmu17: int
    pmu2a: int
    pmu30: int


def _set_charging(rom: Rom, p17: int, on: bool) -> int:
    want = p17 | CHARGE_ON if on else p17 & ~CHARGE_ON
    if want != p17:
        rom.pmu_write(PMU_CHARGE, want)
    return want


def sample(rom: Rom, kind: str, before: bool | None = None, after: bool | None = None,
           ack_events: bool = False) -> Sample:
    """The charger set to BEFORE (if not None), a reading, the events
    acknowledged if asked, then the charger set to AFTER (if not None)"""
    p17 = rom.pmu_read(PMU_CHARGE)
    if before is not None:
        p17 = _set_charging(rom, p17, before)
    mv = read_millivolts(rom)
    p2a = rom.pmu_read(PMU_POWER)
    p30 = rom.pmu_read(PMU_EVENTS)
    if ack_events and p30:
        with suppress(DeviceError):     # failed once as the player re-entered recovery
            rom.pmu_write(PMU_EVENTS, p30)
    if after is not None:
        _set_charging(rom, p17, after)
    return Sample(kind, mv, p17, p2a, p30)


class ChargeLog:
    """One charge run (see the module docstring). The device lock is held
    only while sampling, so other tools can use the player in between.

    With a REST_TARGET_MV the full event does not stop it: it charges on
    (the charger's CV tier caps the cell) until a rest reading reaches the
    target or MAX_HOURS pass, and ends with the charger off, ready for a
    discharge run to start."""

    def __init__(self, session: Session, out: Path, target_mv: int | None = None,
                 max_hours: float | None = None, echo: Callable[[str], None] = print) -> None:
        self.session = session
        self.out = out
        self.target_mv = target_mv
        self.max_s = max_hours * 3600 if max_hours else 6 * 3600 if target_mv else MAX_CHARGE_S
        self.echo = echo
        self.t0 = time.monotonic()

    def sample(self, kind: str, **kw: bool | None) -> Sample:
        with self.session() as rom:
            return sample(rom, kind, kw.get("before"), kw.get("after"),
                          bool(kw.get("ack_events")))

    def retry(self, kind: str, **kw: bool | None) -> Sample | None:
        """None if the player stays unreachable (tried again next period)"""
        deadline = time.monotonic() + RETRY_S
        while True:
            try:
                return self.sample(kind, **kw)
            except (DeviceError, DeviceBusy, OSError) as e:
                self.echo(f"{kind}: {e}")
                if time.monotonic() >= deadline:
                    return None
                time.sleep(10)

    def run(self, wait_hours: float) -> None:
        self.echo(f"waiting up to {wait_hours} h for ROM USB")
        if not wait_rom(time.monotonic() + wait_hours * 3600):
            raise DeviceError("no ROM USB device")
        self.t0 = time.monotonic()
        with self.out.open("a", newline="") as f:
            w = csv.writer(f)

            def log(s: Sample) -> None:
                w.writerow([datetime.now(UTC).isoformat(timespec="seconds"),
                            int(time.monotonic() - self.t0), s.kind, s.mv,
                            f"{s.pmu17:#04x}", f"{s.pmu2a:#04x}", f"{s.pmu30:#04x}"])
                f.flush()
                self.echo(str(s))

            self._charge(log)
        self.echo(f"done: {(time.monotonic() - self.t0) / 3600:.2f} h")

    def _charge(self, log: Callable[[Sample], None]) -> None:
        log(self.sample("found"))                   # the charger as found
        first = self.sample("start", before=True, ack_events=True)
        log(first)
        if not first.pmu2a & EXT_POWER:
            raise DeviceError("no external power (PMU 0x2a bit 2): plug the player into USB")
        latched = first.pmu30 & CHARGE_FULL         # a full event from before
        next_probe = time.monotonic() + PROBE_EVERY
        full_at = None
        while True:
            time.sleep(PERIOD)
            now = time.monotonic()
            if full_at is not None:
                if row := self.retry("rest", before=False, ack_events=True):
                    log(row)
                if now - full_at >= REST_AFTER_FULL:
                    return
                continue
            row = self.retry("charge", before=True, ack_events=True)
            if row is None:
                continue
            log(row)
            reason = ("full" if row.pmu30 & CHARGE_FULL and not latched and not self.target_mv
                      else "limit" if row.mv >= SAFETY_MV
                      else "timeout" if now - self.t0 >= self.max_s else None)
            if reason:
                self._stop(reason, log)
                if self.target_mv:
                    return
                full_at = now
            elif now >= next_probe:
                if self._probe(log):
                    return
                next_probe = time.monotonic() + PROBE_EVERY

    def _stop(self, reason: str, log: Callable[[Sample], None]) -> None:
        for _ in range(60):             # the charger must go off: keep trying
            if stop := self.retry(reason, before=False):
                log(stop)
                return
        raise DeviceError("could not switch charging off")

    def _probe(self, log: Callable[[Sample], None]) -> bool:
        """A near-rest reading with the charger off a while; True once it
        reaches the target. In target mode the charger stays off for it and
        the next charge sample turns it back on."""
        row = self.retry("pause", after=False)
        if not row:
            return False
        log(row)
        time.sleep(PROBE_PAUSE)
        row = self.retry("rest", after=None if self.target_mv else True)
        if not row:
            return False
        log(row)
        if self.target_mv and row.mv >= self.target_mv and not row.pmu17 & CHARGE_ON:
            self.echo(f"target {self.target_mv} mV reached at rest")
            return True
        return False
