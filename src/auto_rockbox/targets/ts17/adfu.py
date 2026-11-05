"""The Actions boot ROM's download mode (ADFU, USB 10d6:10d6): bulk-only
mass-storage framing (CBW, data, CSW) carrying vendor CDBs that read and write
RAM and register a hook (gadget.py). Stock firmware's USB disk speaks the same
framing (stock.py), so the link is shared.

This module touches RAM only: flash, resets and the charger go through the
gadget, which runs on the player's CPU.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import usb.core
import usb.util

from ...errors import ArbError

VID = 0x10D6
PID_ADFU = 0x10D6               # boot ROM download mode
EP_IN, EP_OUT = 0x81, 0x02      # the ROM's bulk endpoints
INTERFACE = 0

CBW_SIG = 0x43425355            # "USBC"
CSW_SIG = 0x53425355            # "USBS"
CBW_DIR_IN = 0x80
ADFU_CDB = 0xCD                 # vendor CDB: <B cmd:I length:I addr:I 3x>
CMD_READ_RAM = 0x93
CMD_WRITE_RAM = 0x13
CMD_SWITCH = 0x20               # registers ADDR as the USB loop's hook (0x21 execute is rejected)
ROM_BLOCK = 0x200               # the ROM handler's largest transfer
READ_CHUNK = 8 * ROM_BLOCK      # whole-packet reads: a smaller buffer risks overflow
TIMEOUT_MS = 1000
DRAIN_TIMEOUT_MS = 50


class Ts17Error(ArbError):
    """A failure of the TS17 tools: transport, protocol or a refused request"""


def present(vid: int = VID, pid: int = PID_ADFU) -> tuple[str, str] | None:
    """The USB device VID:PID as (sysfs name, devnum) if the PC sees it now.
    Read from sysfs only, so it never disturbs the device: a new devnum is how
    a re-enumeration shows."""
    want = (f"{vid:04x}", f"{pid:04x}")
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            ids = ((d / "idVendor").read_text().strip(), (d / "idProduct").read_text().strip())
            if ids == want:
                return d.name, (d / "devnum").read_text().strip()
        except OSError:
            continue
    return None


def wait_present(pid: int, timeout: float) -> float | None:
    """Seconds until 10d6:PID shows up, or None after TIMEOUT"""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if present(VID, pid):
            return time.monotonic() - t0
        time.sleep(0.2)
    return None


class BulkLink:
    """CBW/CSW transactions with 10d6:PID over interface 0. Replies are a byte
    stream (one bulk read can hold data and the CSW), hence `pending`."""

    def __init__(self, pid: int, wait_s: float) -> None:
        deadline = time.monotonic() + wait_s
        dev: Any = usb.core.find(idVendor=VID, idProduct=pid)
        while dev is None and time.monotonic() < deadline:
            time.sleep(0.2)
            dev = usb.core.find(idVendor=VID, idProduct=pid)
        if dev is None:
            raise Ts17Error(f"10d6:{pid:04x} is not connected")
        self.dev = dev
        try:
            if dev.is_kernel_driver_active(INTERFACE):
                dev.detach_kernel_driver(INTERFACE)
            usb.util.claim_interface(dev, INTERFACE)
        except usb.core.USBError as e:
            usb.util.dispose_resources(dev)
            hint = " (no permission: a udev rule for it is needed)" if e.errno == 13 else ""
            raise Ts17Error(f"cannot claim 10d6:{pid:04x}: {e}{hint}") from e
        self.ep_in, self.ep_out = self.endpoints()
        self.pending = b""
        self.patience_s = 0.0
        self._drain()

    def endpoints(self) -> tuple[int, int]:
        """The bulk IN and OUT endpoint addresses"""
        return EP_IN, EP_OUT

    def __enter__(self) -> Self:
        return self

    def __exit__(self, et: type[BaseException] | None, ev: BaseException | None,
                 tb: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        # Releases the interface too, quietly if a launch or reset already
        # took the device off the bus
        usb.util.dispose_resources(self.dev)

    def _drain(self) -> None:
        """Drops a reply an earlier session gave up on (e.g. a read sent while
        a long gadget held the ROM's loop), which would end the next one"""
        while True:
            try:
                self.dev.read(self.ep_in, READ_CHUNK, DRAIN_TIMEOUT_MS)
            except usb.core.USBError:
                return

    @contextmanager
    def patience(self, seconds: float) -> Generator[None]:
        """Reads keep waiting up to SECONDS: while a gadget runs with IRQs
        masked the ROM answers nothing until it returns"""
        self.patience_s = seconds
        try:
            yield
        finally:
            self.patience_s = 0.0

    def send(self, data: bytes) -> None:
        try:
            done = self.dev.write(self.ep_out, data, TIMEOUT_MS)
        except usb.core.USBError as e:
            raise Ts17Error(f"send: {e}") from e
        if done != len(data):
            raise Ts17Error(f"short send: {done} of {len(data)} bytes")

    def recv(self, n: int) -> bytes:
        deadline = time.monotonic() + self.patience_s
        while len(self.pending) < n:
            try:
                got = bytes(self.dev.read(self.ep_in, READ_CHUNK, TIMEOUT_MS))
            except usb.core.USBTimeoutError as e:
                if time.monotonic() < deadline:
                    continue
                raise Ts17Error("recv: timed out") from e
            except usb.core.USBError as e:
                raise Ts17Error(f"recv: {e}") from e
            if not got:
                raise Ts17Error("recv: empty read")
            self.pending += got
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def cbw(self, length: int, cdb: bytes, data_in: bool = False) -> None:
        """A command block; the ROM wants tag 0 (and echoes it in the CSW)"""
        self.send(struct.pack("<IIIBBB16s", CBW_SIG, 0, length,
                              CBW_DIR_IN if data_in else 0, 0, len(cdb), cdb))

    def csw(self) -> None:
        sig, tag, residue, status = struct.unpack("<IIIB", self.recv(13))
        if sig != CSW_SIG or tag != 0:
            raise Ts17Error(f"bad CSW: signature {sig:08x}, tag {tag:x}")
        if status:
            raise Ts17Error(f"the device reported status {status} (residue {residue})")

    def transact_in(self, cdb: bytes, n: int) -> bytes:
        self.cbw(n, cdb, data_in=True)
        data = self.recv(n)
        self.csw()
        return data


def adfu_cdb(cmd: int, length: int, addr: int) -> bytes:
    return struct.pack("<BIII3x", ADFU_CDB, cmd, length, addr)


class Adfu(BulkLink):
    """The boot ROM in download mode: RAM reads and writes, and the hook"""

    def __init__(self, wait_s: float = 5.0) -> None:
        super().__init__(PID_ADFU, wait_s)

    def read(self, addr: int, n: int) -> bytes:
        out = bytearray()
        for off in range(0, n, ROM_BLOCK):
            k = min(ROM_BLOCK, n - off)
            out += self.transact_in(adfu_cdb(CMD_READ_RAM, k, addr + off), k)
        return bytes(out)

    def write(self, addr: int, data: bytes) -> None:
        for off in range(0, len(data), ROM_BLOCK):
            chunk = data[off:off + ROM_BLOCK]
            self.cbw(len(chunk), adfu_cdb(CMD_WRITE_RAM, len(chunk), addr + off))
            self.send(chunk)
            self.csw()

    def switch(self, addr: int) -> bool:
        """Registers ADDR (even; the ROM adds the Thumb bit) as the hook its
        USB loop calls continuously. True if a clean CSW came back; the ROM
        does not always send one, and the hook works either way."""
        self.cbw(0, adfu_cdb(CMD_SWITCH, 0, addr))
        try:
            self.csw()
        except Ts17Error:
            return False
        return True
