"""The YP3's torture run: the shared Rockbox torture (suites/rockbox_torture.py)
with what only the YP3's shell and hardware have.

    arb yp3 torture build/torture-elf/rockbox.elf build/torture/RUN --hours 6

Each boot pushes the settings files to /yp3dev/ with the image (the flash
bundle), turns the speaker off and sets Charge Limit 80 %. A crash
is collected from the SRAM a watchdog reset keeps (fault, threads, log). The
YP3 reads its LCD controller's clock gate (`sleep`), tunes without the radio
screen (FM recordings), shows its controller's link table and LMP trace
after a failed Bluetooth connect, and has the `fm` scenario (`fm`, `fmregs`,
`fmmon`: the tuner and its capture).
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ...elf import Elf
from ...suites.rockbox_torture import (
    BT_HID,
    FM_KHZ,
    FM_TUNE,
    LCD_GATE,
    SINK_BT,
    RockboxTorture,
    parse_bt_status,
    r32_words,
)
from ...suites.torture import Lost, Records, player_suite, scenario
from ...target import RunRequest, Target
from . import Yp3, Yp3Run
from .retained import decode_fault, decode_log, decode_threads
from .rom import wait_rom

YP3 = {"yp3"}

# known too slow or over a documented limit (docs/codecs.md: Vorbis pages over
# ~10 KB, Opus pages over 30 KB)
SLOW_CODECS = ("wv_24", "vorbis_q", "vorbis_long", "vq3_1s", "opus_510")
# Error messages the YP3's heap limits provoke on purpose
EXPECTED_MSG = {"record": re.compile(r"(?i)not enough memory"),
                "recui": re.compile(r"(?i)not enough memory"),
                # music holds the heap the database menus need: the prompt
                "db": re.compile(r"(?i)not enough memory for / the database"),
                "dbdeep": re.compile(r"(?i)not enough memory for / the database|"
                                     r"selection too big"),
                # cabbiev2 leaves too little for Vorbis or Opus
                "theme": re.compile(r"(?i)not enough memory to play|leaves little memory")}
CHARGE_LIMIT = 80               # % (in t_default.cfg)
LCD_GATE_ACK = 0x40080068       # clock gate 0x5c (LCD controller): bit 28
BT_LINK_TBL = 0x804128          # ROM link address table: 7 x {u8 in use, BD_ADDR}


def fm_periods(text: str) -> tuple[int, int] | None:
    """`fmmon`: (capture periods, overruns)"""
    m = re.search(r"periods=(\d+) consumed=\d+ over=(\d+)", text)
    return (int(m[1]), int(m[2])) if m else None


def fm_consumer(m0: str, m1: str) -> str:
    """What drained the capture between two `fmmon` replies: the mixer
    channel, the DAC, the mixer's longest gap"""
    d0, d1 = (re.search(r"mixer chan=(\d+) pcm playing=(\d+) dac periods=(\d+)", m)
              for m in (m0, m1))
    gaps = re.search(r"mixer gaps: longest (\d+) ms, (\d+) over two periods, "
                     r"last call (\d+) ms ago", m1)
    why = ""
    if d0 and d1:
        chan = ("stopped", "playing", "paused")[int(d1[1])] if int(d1[1]) < 3 else d1[1]
        why += (f"; mixer channel {chan}, pcm playing {d1[2]}, "
                f"DAC periods +{int(d1[3]) - int(d0[3])}")
    if gaps:
        why += (f"; longest mixer gap {gaps[1]} ms, {gaps[2]} over two periods, "
                f"last call {gaps[3]} ms ago")
    return why


def link_table(words: list[int]) -> list[str]:
    """The BD_ADDRs in use in the controller's link address table (BT_LINK_TBL)"""
    raw = b"".join(w.to_bytes(4, "little") for w in words)
    return [":".join(f"{b:02X}" for b in reversed(raw[i + 1:i + 7]))
            for i in range(0, min(len(raw), 49) - 6, 7) if raw[i] == 1]


@player_suite("yp3")
class Yp3Torture(RockboxTorture):
    """The torture run on the YP3. Config: as RockboxTorture's ([targets.yp3]
    bt_address, menu_coverage)."""

    card_dir = "/yp3dev"            # yp3.h DEV_CARD_DIR
    slow_codecs = SLOW_CODECS
    expected_msgs = EXPECTED_MSG
    charge_limit = CHARGE_LIMIT
    features = frozenset({LCD_GATE, FM_TUNE, BT_HID})
    crash_lines = r"\bHANG:"        # the heartbeat watchdog's last word

    def __init__(self, target: Target, image: Path, **kw: Any) -> None:
        super().__init__(target, image, **kw)
        if not isinstance(target, Yp3):
            raise TypeError(f"{target.name}: not a YP3")
        self.yp3 = target
        self.shell = target.shell(image)        # names the addresses in `ticks:` by IMAGE

    # ---- boots and crashes

    def boot_settings(self) -> dict[str, str]:
        return {"speaker": "0"}

    def boot_request(self) -> RunRequest:
        pushes = []
        for name, text in self.cfgs().items():
            (self.out / name).write_text(text)
            pushes.append((self.out / name, self.card_path(name)))
        return Yp3Run(self.image, 0, usb=True, settings=self.boot_settings(), pushes=pushes)

    def put_card_files(self) -> None:
        """Every boot pushes them (boot_request)"""

    def wait_host(self, timeout: float) -> bool:
        return wait_rom(time.monotonic() + timeout) is not None

    def records(self) -> Records:
        raw = self.yp3.read_retained()
        names = self.yp3.function_names(self.image)
        log = decode_log(raw, names)
        return [("fault", decode_fault(raw, self.yp3.symbolizer(self.image).names)),
                ("threads", decode_threads(raw, Elf(self.image), names)),
                ("dev log (tail)", "\n".join(log.splitlines()[-120:]))]

    # ---- the YP3's own commands and registers

    def gate_on(self) -> bool:
        words = r32_words(self.sh(f"r32 {LCD_GATE_ACK:#x}"))
        return bool(words and words[0] >> 28 & 1)

    def fm_tune(self, khz: int | None) -> None:
        self.sh(f"fm {khz}" if khz else "fm off")

    def bt_link_table(self) -> list[str]:
        return link_table(r32_words(self.sh(f"r32 {BT_LINK_TBL:#x} 13")))

    def bt_fail_details(self) -> list[str]:
        """The LMP trace (its ring outlasts the 4 KB log) and the link table"""
        out = ["--- sh bt ctrl lmp"]
        try:
            out += self.sh("bt ctrl lmp")
        except Lost as e:
            out.append(f"(no reply: {e})")
        try:
            out.append(f"--- link table in use: {' '.join(self.bt_link_table()) or 'none'}")
        except Lost as e:
            out.append(f"--- link table: no reply: {e}")
        return out


@scenario("fm", weight=6, target=YP3)
def fm(t: Yp3Torture) -> str:
    """Tune, read the tuner over I2C, run the FM monitor a while: the capture
    keeps running without new overruns (but on a Bluetooth headset, no real
    use: the radio needs wired headphones as its antenna)"""
    khz = t.rng.choice(FM_KHZ)
    t.sh("stop")                        # as the radio screen does
    h = t.health()
    t.fm_tune(khz)
    regs = " ".join(t.sh("fmregs"))
    m0 = " ".join(t.sh("fmmon"))
    time.sleep(t.rng.uniform(5, 20))
    m1 = " ".join(t.sh("fmmon"))
    t.fm_tune(None)
    p0, p1 = fm_periods(m0), fm_periods(m1)
    if "r=-1" in regs or not regs:
        t.fail(f"fmregs: {regs[:100]}")
    elif not p0 or not p1 or p1[0] <= p0[0]:
        t.fail(f"capture not running: {m1[:100]}")
    elif p1[1] > p0[1]:
        bt = parse_bt_status(t.sh("bt stack status"))
        if not (bt and bt.sink == SINK_BT):
            t.fail(f"capture overruns +{p1[1] - p0[1]}" + fm_consumer(m0, m1))
    t.check_health(h)
    return f"{khz} kHz"
