"""This PC's side of a Bluetooth link, through BlueZ's and PipeWire's own
tools: whether a player is connected, paging it, its A2DP input node, moving
its stream to a sink, the link's RSSI, and the capture (HCI trace and RSSI)
of a player walk, and the adapter back from a Bumble peer.

Stock players often cannot connect to a PC by themselves (the PC refuses
their role switch) and do not page again after a drop, so the PC pages them:
`page` finds, trusts and connects a discoverable player, and `watch_link`
pages one that dropped. btmon and btmgmt need root; bluetoothctl, pw-dump
and pactl do not.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import usb.core

from . import rundir

SCAN_S = 8              # inquiry before each look for a player being paged
CONNECT_S = 35          # bluetoothctl's wait for a connect (BlueZ pages ~5 s a try)
POLL_S = 10.0
PAGE_FOR_S = 300.0      # how long a dropped player is paged
PAGE_EVERY_S = 20.0


def _run(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    """ARGV's result, or None if it could not run or hung"""
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


# ---------------------------------------------------------------- bluetoothctl

def connected(addr: str) -> bool | None:
    """Whether BlueZ has a link to ADDR; None if bluetoothctl did not answer"""
    r = _run(["bluetoothctl", "info", addr], 20)
    return None if r is None else "\tConnected: yes" in r.stdout


def known(addr: str) -> bool:
    """Whether BlueZ has seen ADDR (a scan found it, or it is paired)"""
    r = _run(["bluetoothctl", "info", addr], 20)
    return r is not None and r.returncode == 0


def scan(seconds: float = SCAN_S) -> None:
    _run(["bluetoothctl", "--timeout", str(round(seconds)), "scan", "on"], seconds + 10)


def trust(addr: str) -> None:
    _run(["bluetoothctl", "trust", addr], 20)


def connect(addr: str, timeout: float = CONNECT_S) -> bool:
    """One connect (BlueZ pages ADDR); True if the link is up after it"""
    _run(["bluetoothctl", "--timeout", str(round(timeout)), "connect", addr], timeout + 10)
    return bool(connected(addr))


def page(addr: str, tries: int = 60, say: Callable[[str], object] = print) -> bool:
    """Finds, trusts and connects ADDR, a player waiting discoverable (stock
    on its Bluetooth menu); True once connected. A player not paired yet
    needs a pairing agent on this PC: the desktop's asks in a dialog."""
    for _ in range(tries):
        if connected(addr):
            say("connected")
            return True
        scan()
        if known(addr):
            say("seen; trust and connect")
            trust(addr)
            connect(addr)
    say("gave up")
    return False


# ---------------------------------------------------------------- PipeWire

def _mentions(props: dict[str, Any], addr: str) -> bool:
    """Whether PipeWire properties name ADDR (as AA:BB:.. or AA_BB_..)"""
    text = json.dumps(props).upper()
    return addr.upper() in text or addr.replace(":", "_").upper() in text


def input_node(addr: str | None = None) -> str | None:
    """The PipeWire node of a player's A2DP stream into this PC (BlueZ's
    bluez_input.*), or None; with ADDR, only that device's"""
    r = _run(["pw-dump"], 10)
    try:
        objs = json.loads(r.stdout) if r else []
    except ValueError:
        return None
    for o in objs:
        if not str(o.get("type", "")).endswith("Node"):
            continue
        props = (o.get("info") or {}).get("props") or {}
        if (props.get("device.api") == "bluez5"
                and props.get("media.class") in ("Audio/Source", "Stream/Output/Audio")
                and (addr is None or _mentions(props, addr))):
            return props.get("node.name")
    return None


def sink_index(name: str) -> str | None:
    """The index of the PulseAudio sink NAME, or None if there is none"""
    r = _run(["pactl", "list", "short", "sinks"], 20)
    lines = r.stdout.splitlines() if r else []
    return next((ln.split("\t")[0] for ln in lines if ln.split("\t")[1:2] == [name]), None)


def route(addr: str, sink: str) -> None:
    """Moves the player's stream (a loopback into the default sink) to SINK,
    so nobody hears it. Best effort: a failed move is retried at the next
    poll and must not end a long watch."""
    target = sink_index(sink)
    r = _run(["pactl", "-f", "json", "list", "sink-inputs"], 20)
    try:
        inputs = json.loads(r.stdout or "[]") if r else []
    except ValueError:
        return
    for si in inputs:
        if _mentions(si.get("properties") or {}, addr) and str(si.get("sink")) != target:
            _run(["pactl", "move-sink-input", str(si.get("index")), sink], 20)


# ---------------------------------------------------------------- watch

@dataclass(frozen=True)
class LinkEvent:
    at: datetime
    kind: Literal["poll", "up", "down", "page", "back", "over"]
    connected: bool


def watch_link(addr: str, *, period: float = POLL_S, page_for: float = PAGE_FOR_S,
               page_every: float = PAGE_EVERY_S, sink: str | None = None
               ) -> Iterator[LinkEvent]:
    """BlueZ's view of the link to ADDR: "poll" every PERIOD s with its
    state, "up" the first time it is connected, "down" when it drops. After
    a drop the PC pages the player every PAGE_EVERY s ("page", then a "poll")
    and says "back" once it answers, then watches on; if it has not answered
    after PAGE_FOR s, "over" ends the watch. PAGE_FOR 0: never page (a player
    that reconnects by itself), only say "back". SINK: keep the player's
    stream on that sink while connected. The consumer decides when to stop."""
    seen = False
    while True:
        up = connected(addr)
        if up is None:
            time.sleep(period)
            continue
        yield LinkEvent(rundir.now(), "poll", up)
        if up:
            if not seen:
                seen = True
                yield LinkEvent(rundir.now(), "up", True)
            if sink:
                route(addr, sink)
        if up or not seen:
            time.sleep(period)
            continue
        yield LinkEvent(rundir.now(), "down", False)
        deadline = time.monotonic() + page_for
        while True:
            if page_for:
                yield LinkEvent(rundir.now(), "page", False)
                connect(addr)
            back = bool(connected(addr))
            yield LinkEvent(rundir.now(), "poll", back)
            if back:
                break
            if page_for and time.monotonic() >= deadline:
                yield LinkEvent(rundir.now(), "over", False)
                return
            time.sleep(page_every if page_for else period)
        yield LinkEvent(rundir.now(), "back", True)
        if sink:
            route(addr, sink)


# ---------------------------------------------------------------- RSSI and capture

@dataclass(frozen=True)
class ConnInfo:
    rssi: int                       # relative to the adapter's golden range: 0 is in it
    tx_power: int
    max_tx_power: int

    def line(self) -> str:
        return f"rssi {self.rssi} tx {self.tx_power} max {self.max_tx_power}"


_CONN_INFO = re.compile(r"RSSI (-?\d+)\s+TX power (-?\d+)\s+maximum TX power (-?\d+)")
_RSSI_LINE = re.compile(r"rssi (-?\d+) tx (-?\d+) max (-?\d+)")


def parse_conn_info(text: str) -> ConnInfo | None:
    """btmgmt conn-info's answer; None for "Not Connected" and other failures"""
    m = _CONN_INFO.search(text)
    return ConnInfo(*map(int, m.groups())) if m else None


def parse_rssi_line(text: str) -> ConnInfo | None:
    """A pc-rssi.txt line's reading (ConnInfo.line()); None for "none" """
    m = _RSSI_LINE.fullmatch(text)
    return ConnInfo(*map(int, m.groups())) if m else None


def conn_info(addr: str) -> ConnInfo | None:
    """The PC's RSSI and TX power on its BR/EDR link to ADDR (root)"""
    r = _run(["btmgmt", "conn-info", "-t", "0", addr], 10)
    return parse_conn_info(r.stdout + r.stderr) if r else None


def usb_ids(adapter: str) -> tuple[int, int] | None:
    """VID and PID of a Bumble transport usb:VID:PID (hex); None for any other"""
    m = re.fullmatch(r"usb:([0-9a-fA-F]{4}):([0-9a-fA-F]{4})", adapter)
    return (int(m[1], 16), int(m[2], 16)) if m else None


def give_back(adapter: str, reset: bool = False) -> None:
    """ADAPTER (usb:VID:PID) back to its kernel driver, btusb: libusb does
    this when Bumble closes the adapter, a killed peer does not. RESET: a
    USB reset instead, so btusb probes it afresh (a wedged controller)."""
    ids = usb_ids(adapter)
    dev: Any = usb.core.find(idVendor=ids[0], idProduct=ids[1]) if ids else None
    if dev is None:
        return
    with suppress(usb.core.USBError, NotImplementedError):
        if reset:
            dev.reset()
        elif not dev.is_kernel_driver_active(0):
            dev.attach_kernel_driver(0)


def _hand_back(paths: list[Path]) -> None:
    """Under sudo, gives the capture's files to the user who ran it"""
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if uid is None or gid is None:
        return
    for p in paths:
        if p.exists():
            os.chown(p, int(uid), int(gid))


def capture(out: Path, addr: str, period: float = 2.0,
            echo: Callable[[str], object] | None = print) -> None:
    """The PC's side of a player walk until Ctrl-C: btmon's HCI trace into
    OUT/pc.btsnoop and the link's RSSI every PERIOD s into OUT/pc-rssi.txt.
    Needs root."""
    created = not out.exists()
    out.mkdir(parents=True, exist_ok=True)
    trace, rssi = out / rundir.TRACE, out / rundir.RSSI
    with subprocess.Popen(["btmon", "-w", str(trace)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL) as mon:
        try:
            with rundir.Log(rssi, echo) as log:
                log.comment(f"btmgmt conn-info {addr} every {period:g} s")
                while mon.poll() is None:
                    ci = conn_info(addr)
                    log(ci.line() if ci else "none")
                    time.sleep(period)
        finally:
            mon.terminate()
            mon.wait()
            _hand_back(([out] if created else []) + [trace, rssi])
