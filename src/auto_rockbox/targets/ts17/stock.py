"""Stock firmware's USB disk (10d6:b00b) into download mode: the way back when
a reset booted stock (the boot-mode magic was clear) and no hook is left to
catch it. Actions vendor SCSI commands in 6-byte CDBs over raw USB; the
kernel's usb-storage is detached from the disk for it.
"""

from __future__ import annotations

import usb.util

from .adfu import PID_ADFU, BulkLink, Ts17Error, wait_present

PID_STOCK = 0xB00B                       # stock firmware's USB disk
CDB_IDENTIFY = bytes([0xCC, 0, 0, 0, 0, 0])
IDENTITY = b"ACTIONSUSBD"                # its reply to CDB_IDENTIFY
CDB_TO_ADFU = bytes([0xCB, 0, 0, 0, 0, 0])   # reboot into download mode; replies 2 bytes
ADFU_WAIT_S = 30.0                       # it is back ~0.6 s after the reply


class StockDisk(BulkLink):
    def __init__(self, wait_s: float = 5.0) -> None:
        super().__init__(PID_STOCK, wait_s)

    def endpoints(self) -> tuple[int, int]:
        intf = self.dev[0][(0, 0)]
        found = {}
        for e in intf:
            if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK:
                found[usb.util.endpoint_direction(e.bEndpointAddress)] = int(e.bEndpointAddress)
        if len(found) != 2:
            raise Ts17Error("stock firmware's USB disk has no bulk endpoint pair")
        return found[usb.util.ENDPOINT_IN], found[usb.util.ENDPOINT_OUT]


def to_download_mode() -> float:
    """Seconds until download mode is on the bus. Sends the reboot only after
    the disk identified itself as Actions' (CDB_IDENTIFY)."""
    with StockDisk() as disk:
        ident = disk.transact_in(CDB_IDENTIFY, len(IDENTITY))
        if ident != IDENTITY:
            raise Ts17Error(f"unexpected identity {ident!r}: not sending the reboot")
        disk.transact_in(CDB_TO_ADFU, 2)
    t = wait_present(PID_ADFU, ADFU_WAIT_S)
    if t is None:
        raise Ts17Error(f"no download mode within {ADFU_WAIT_S:.0f} s")
    return t
