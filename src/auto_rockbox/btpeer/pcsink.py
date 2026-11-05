"""bt-peer pcsink: this PC's adapter as the player's A2DP sink through
BlueZ, and the analysis of what the player streams to it (the player-side
counterpart of bt-peer sink). No Bumble: bluetoothctl, pactl and pw-record.

1. A bluetoothctl agent (`agent`, default NoInputNoOutput) powers the
   adapter, makes it discoverable and pairable, answers its confirm and
   authorize prompts with yes and trusts any device whose name starts with
   `name`.
2. A null sink is the default sink while it runs (the old default comes
   back afterwards), so the stream does not play on the speakers. Once the
   player's PipeWire node (BlueZ's A2DP sink role) appears, `seconds` s of
   the null sink are recorded with pw-record and analysed like bt-peer
   sink's stream: level, tone and purity per second, dropouts.

On the player: Bluetooth on, connected to the adapter address this prints,
the Bluetooth sink selected, then a tone playing. Discoverable is switched
off again at the end. Outputs in OUT: bluetoothctl.log, stream.wav.
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import IO, NoReturn, Self

from ..bt import bluez
from ..errors import ArbError
from . import analysis
from .options import PcSinkOptions

TOOLS = ("bluetoothctl", "pactl", "pw-record")
DEFAULT_AGENT_S = 10.0
RATE = 44100

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")
_FOUND = re.compile(r"Device ([0-9A-F:]{17}) (.*)")


class Agent:
    """An interactive bluetoothctl as BlueZ's default pairing agent with
    CAPABILITY: it answers confirm and authorize prompts with yes and trusts
    each device whose name starts with NAME; LOG gets what it said. Until it
    is the default agent, pairing requests go to the desktop's agent (KDE's
    is DisplayYesNo: the PC then wants MITM, asks that agent and waits on
    its dialog). DISCOVERABLE: the adapter takes a page from a device it
    does not know only while discoverable (page scan follows the paired
    devices). close() switches discoverable off and quits."""

    def __init__(self, name: str, capability: str, log: IO[str], *,
                 discoverable: bool = True, say: Callable[[str], object] = print) -> None:
        self.name, self.log, self.say = name, log, say
        self.trusted: set[str] = set()
        self._default = threading.Event()
        self._lock = threading.Lock()
        self.proc = subprocess.Popen(["bluetoothctl"], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        if self.proc.stdin is None or self.proc.stdout is None:
            raise ArbError("bluetoothctl: no pipes")
        self._stdin, self._stdout = self.proc.stdin, self.proc.stdout
        threading.Thread(target=self._read, daemon=True).start()
        for cmd in ("power on", f"agent {capability}", "default-agent", "pairable on",
                    "discoverable on" if discoverable else "discoverable off"):
            self.send(cmd)
        if not self._default.wait(DEFAULT_AGENT_S):
            say(f"agent: bluetoothctl is not the default agent after {DEFAULT_AGENT_S:g} s")
            log.write(f"not the default agent after {DEFAULT_AGENT_S:g} s\n")

    def send(self, line: str) -> None:
        with self._lock:
            self._stdin.write(line.encode() + b"\n")

    def _seen(self, line: str) -> None:
        m = _FOUND.search(line)
        if m and m[2].startswith(self.name) and m[1] not in self.trusted:
            # by a separate bluetoothctl: a line typed into this one while its
            # agent prompt is up would answer the prompt instead
            self.trusted.add(m[1])
            self.say(f'player seen: {m[1]} "{m[2]}"; trusting it')
            threading.Thread(target=bluez.trust, args=(m[1],), daemon=True).start()

    def _read(self) -> None:
        # raw reads: agent prompts end without a newline, and BlueZ cancels
        # the request if the answer only comes with the next line
        buf = ""
        while data := self._stdout.read(4096):
            buf += _ANSI.sub("", data.decode(errors="replace"))
            *lines, buf = buf.split("\n")
            for line in filter(None, (ln.strip() for ln in lines)):
                self.log.write(line + "\n")
                self._seen(line)
                if "Default agent request successful" in line:
                    self._default.set()
            if "(yes/no)" in buf:
                self.say(f"agent: {buf.strip()} -> yes")
                self.log.write(buf.strip() + " -> yes\n")
                self.send("yes")
                buf = ""

    def close(self) -> None:
        try:
            self.send("discoverable off")
            self.send("quit")
        except OSError:
            pass                            # it is gone already
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def adapter_address() -> str:
    try:
        out = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True,
                             timeout=10, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return "?"
    m = re.search(r"Controller ([0-9A-F:]{17})", out)
    return m[1] if m else "?"


def _pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10,
                          check=False).stdout.strip()


@contextmanager
def capture_sink(name: str) -> Generator[None]:
    """A null sink NAME as the default sink while the block runs, so
    PipeWire routes the player's stream (a Stream/Output/Audio node that
    WirePlumber links to the default sink) there instead of the speakers"""
    old = _pactl("get-default-sink")
    module = _pactl("load-module", "module-null-sink", f"sink_name={name}")
    _pactl("set-default-sink", name)
    try:
        yield
    finally:
        if old:
            _pactl("set-default-sink", old)
        if module:
            _pactl("unload-module", module)


def _sigterm(_sig: int, _frame: FrameType | None) -> NoReturn:
    raise SystemExit(1)


@contextmanager
def _unwind_on_sigterm() -> Generator[None]:
    """SIGTERM (timeout, a task stop) unwinds the block, so the default sink
    and discoverable come back"""
    old = signal.signal(signal.SIGTERM, _sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, old)


def record(wav: Path, sink: str, seconds: float) -> None:
    """SECONDS s of SINK's monitor into WAV"""
    with subprocess.Popen(["pw-record", "--target", sink, "-P", "stream.capture.sink=true",
                           "--rate", str(RATE), "--channels", "2", "--format", "s16",
                           str(wav)]) as rec:
        try:
            rec.wait(seconds)
        except subprocess.TimeoutExpired:
            rec.send_signal(signal.SIGINT)      # pw-record then finishes the WAV header
            try:
                rec.wait(10)
            except subprocess.TimeoutExpired:
                rec.kill()


def wait_for_node(seconds: int) -> str | None:
    for _ in range(seconds):
        node = bluez.input_node()
        if node:
            return node
        time.sleep(1)
    return None


def main(opts: PcSinkOptions) -> None:
    missing = [t for t in TOOLS if shutil.which(t) is None]
    if missing:
        raise ArbError(f"bt-peer pcsink needs {', '.join(missing)}")
    opts.out.mkdir(parents=True, exist_ok=True)
    with (_unwind_on_sigterm(), capture_sink(opts.capture_sink),
          (opts.out / "bluetoothctl.log").open("w", buffering=1) as log,
          Agent(opts.name, opts.agent, log)):
        print(f"adapter {adapter_address()} discoverable; waiting up to {opts.wait} s "
              "for the player's A2DP stream")
        node = wait_for_node(opts.wait)
        if not node:
            raise ArbError(f"no Bluetooth input node appeared within {opts.wait} s")
        wav = opts.out / "stream.wav"
        print(f"recording {opts.seconds} s of {node} -> {wav}")
        record(wav, opts.capture_sink, opts.seconds)
    r = analysis.analyse_wav(wav, print)
    print(f"{r.seconds:.1f} s, median tone {r.medians}, {len(r.gaps)} dropouts {r.gaps[:10]}")
