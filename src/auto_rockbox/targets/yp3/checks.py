"""Guided hands-on checks with a person at the player (`arb yp3 checks`): the
keys, the built-in mic, FM, the speaker and power-off on battery. (The
headphone jack has no detect signal: it cuts the speaker in hardware.)
Each step says what to do, shows the player's readings live from its dev
shell, and asks what the person saw or heard.

The checks need the dev build running with its USB serial shell (usb=1). If
none is, the command boots one from ROM USB (with the configured modules)
and sends the player back there afterwards, with the charger guarded as a
run's end does. The speaker step plays the card's /yp3dev/tone.mp3.
"""

from __future__ import annotations

import re
import sys
import time
from argparse import Namespace
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ...errors import ArbError
from ...registry import Context, arg, command
from ...shell import NoShell, Reply
from . import Yp3, Yp3Run

STEPS = ("keys", "mic", "fm", "speaker", "poweroff")
KEYS = {0x01: "UP", 0x02: "DOWN", 0x04: "LEFT", 0x08: "RIGHT", 0x10: "CENTRE"}   # button-target.h
TONE = "/yp3dev/tone.mp3"
MIC_REC = "mic"                 # the mic step's recording: /yp3dev/mic.wav

_BUTTONS = re.compile(r"buttons ([0-9a-f]+)")

type ShellFn = Callable[[str, float, Callable[[str], None] | None], Reply]


def _host_time() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Checks:
    """The steps, over SH (a dev shell: line, timeout, echo for each reply
    line as it comes), with a person answering ASK"""

    sh: ShellFn
    fm_khz: int = 97100
    ask: Callable[[str], str] = input
    echo: Callable[[str], object] = print
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], float] = time.monotonic
    now: Callable[[], str] = _host_time

    def prompt(self, text: str) -> None:
        self.ask(f"\n>>> {text}")

    def yes(self, question: str) -> bool:
        return self.ask(f"\n>>> {question} [y/n] ").strip().lower().startswith("y")

    def lines(self, line: str, timeout: float = 60.0) -> list[str]:
        """The reply to LINE but its closing ok"""
        return [x for x in self.sh(line, timeout, None).lines if x and x != "ok"]

    def first(self, line: str, timeout: float = 60.0) -> str:
        return next(iter(self.lines(line, timeout)), "(no reply)")

    def wait_shell(self, seconds: float) -> bool:
        """Until the dev shell answers, for up to SECONDS (0: one try)"""
        end = self.clock() + seconds
        while True:
            try:
                if self.sh("uptime", 10.0, None).ok:
                    return True
            except (NoShell, OSError):
                pass
            if self.clock() >= end:
                return False
            self.sleep(2.0)

    def run(self, steps: Iterable[str]) -> dict[str, bool]:
        """Each step's outcome, in turn"""
        by_name = {"keys": self.keys, "mic": self.mic, "fm": self.fm,
                   "speaker": self.speaker, "poweroff": self.poweroff}
        return {s: by_name[s]() for s in steps}

    def keys(self) -> bool:
        self.prompt("Keys: press Enter here, then on the player press UP, DOWN, LEFT, RIGHT "
                    "and the centre key one at a time (hold each ~1 s). Each change prints "
                    "live for 20 s.")
        seen: set[str] = set()

        def show(line: str) -> None:
            if m := _BUTTONS.match(line):
                pressed = [n for bit, n in KEYS.items() if int(m.group(1), 16) & bit]
                seen.update(pressed)
                self.echo("    pressed: " + ("+".join(pressed) or "(released)"))
        self.sh("keylog 20", 40.0, show)
        missing = [n for n in KEYS.values() if n not in seen]
        self.echo(f"    seen: {', '.join(sorted(seen)) or 'none'} "
                  f"| missing: {', '.join(missing) or 'none'}")
        return not missing

    def mic(self) -> bool:
        """The built-in mic, by ear: the firmware records it to the card and
        plays the recording back"""
        self.prompt("Mic: press Enter, then talk or tap next to the player for 6 s.")
        reply = self.sh(f"rec 6 {MIC_REC} mic", 40.0, None)
        for line in reply.lines:
            if line != "ok":
                self.echo(f"    {line}")
        if not reply.ok:
            return False
        self.prompt("Plug headphones into the player to hear the recording; press Enter.")
        self.lines("vol -20")
        self.lines(f"play /yp3dev/{MIC_REC}.wav")
        ok = self.yes("Do you hear what the mic picked up?")
        self.lines("stop")
        return ok

    def fm(self) -> bool:
        self.prompt("FM: plug headphones into the player (the headphone cable is the "
                    "antenna); press Enter.")
        self.lines("vol -20")
        self.echo(f"    {self.first(f'fm {self.fm_khz}')}")
        ok = self.yes("Do you hear a radio station (`arb yp3 sh fm KHZ` tunes others)?")
        self.echo("    recording 5 s...")
        self.echo(f"    {self.first('rec 5', 40.0)}")
        self.lines("fm off")
        self.echo("    (the recording is /yp3dev/rec.wav on the card)")
        return ok

    def speaker(self) -> bool:
        self.prompt("Speaker: unplug the headphones; press Enter to play a tone with the "
                    "amplifier on.")
        for line in ("amp 1", "vol -20", f"play {TONE}"):
            self.lines(line)
        ok = self.yes("Do you hear a tone from a built-in speaker?")
        self.lines("amp 0")
        self.lines("stop")
        return ok

    def poweroff(self) -> bool:
        self.echo("\nPower-off on battery:")
        self.prompt("Unplug the USB cable, then hold the centre key until the player switches "
                    "off. Press Enter when it is off (or after ~10 s if it is not).")
        off = self.yes("Did the screen go dark and stay off (no backlight)?")
        self.prompt("Switch it on again (centre key), wait for the menu, then plug USB back "
                    "in. Press Enter.")
        if self.wait_shell(60.0):
            self.echo(f"    player: {self.first('date')}  host: {self.now()}")
        else:
            self.echo("    no serial shell (the dev build attaches only with usb=1 in its "
                      "config)")
        return off


def booted(t: Yp3, checks: Checks, steps: Iterable[str], elf: Path,
           seconds: int) -> dict[str, bool]:
    """CHECKS on ELF, booted from ROM USB with its modules and usb=1 to
    return after SECONDS; the player goes back to ROM USB at the end"""
    req = Yp3Run(elf, seconds, usb=True, pushes=t.modules(elf))
    print("booting the dev build (the player must be in ROM USB)")
    t.to_rom()
    with t.lock():
        t.before_run(req)
        t.launch(req)
        try:
            if not checks.wait_shell(150.0):
                raise NoShell(f"no serial shell after the boot; `arb yp3 log` once the "
                              f"player is back in ROM USB ({seconds} s after it)")
            print("serial shell up")
            return checks.run(steps)
        finally:
            try:
                t.to_rom(timeout=60)
            except ArbError as e:
                print(f"{e}: it returns by itself {seconds} s after its boot, with the charger "
                      "unguarded", file=sys.stderr)
            else:
                t.after_run(req, True)


@command("checks", target={"yp3"}, needs={"shell"},
         help="guided hands-on checks with a person at the player", args=[
             arg("steps", nargs="*", choices=STEPS, metavar="STEP",
                 help=f"{' '.join(STEPS)} (default: all, in this order)"),
             arg("--elf", type=Path,
                 help="the dev build to boot if none runs (default [targets.yp3] elf)"),
             arg("--seconds", type=int, default=1500,
                 help="its return time if this boots it (default 1500)"),
             arg("--fm-khz", type=int, default=97100,
                 help="the station the FM step tunes (default 97100)"),
         ])
def checks_cmd(ctx: Context, args: Namespace) -> int:
    """Uses the running dev build's serial shell, or boots one (and returns
    the player to ROM USB afterwards). Prints each step's outcome; the exit
    status is 0 whatever they are."""
    t = ctx.t
    if not isinstance(t, Yp3):
        raise TypeError(f"{t.name}: not a YP3")
    checks = Checks(t.shell(), args.fm_khz)
    steps = args.steps or STEPS
    if checks.wait_shell(0):
        print("dev build already running with the serial shell")
        results = checks.run(steps)
    else:
        results = booted(t, checks, steps, args.elf or t.default_elf, args.seconds)
    print("\nresults: " + ", ".join(f"{s}={'ok' if ok else 'FAIL'}" for s, ok in results.items()))
    return 0
