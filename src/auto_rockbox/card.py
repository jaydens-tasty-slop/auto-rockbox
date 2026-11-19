"""The player's card on this PC, over Rockbox's USB mass storage, by udisks:
find it, mount it, check it. No root needed.

The check is `fsck.fat` itself (its report; it can also repair) when this
user may open the card's device: a udev rule tags the players' disks
`uaccess` (Rockbox's vendor ID with a pid.codes test product ID; the port's
tools/udev/70-rockbox-cards.rules). Without that rule it is udisks' check:
clean or not, no details, no repair.

The card is found by its filesystem UUID (`[targets.NAME] card_uuid`) or, if
none is set, as the first partition on a USB disk with the player's USB IDs
(Rockbox's vendor ID and the target's product ID: another player on the same
test vendor ID is never taken for it).
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .errors import ArbError

ROCKBOX_VID = "1209"


class CardError(ArbError):
    pass


def _props(dev: Path) -> dict[str, str]:
    out = subprocess.run(["udevadm", "info", "-q", "property", "-n", str(dev)],
                         capture_output=True, text=True, check=False).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def find(uuid: str | None = None, product: str | None = None,
         vendor: str = ROCKBOX_VID) -> Path | None:
    """The card's block device if the PC sees it now: by UUID, or by the
    player's USB IDs (PRODUCT None: any product of VENDOR)"""
    if uuid:
        p = Path("/dev/disk/by-uuid") / uuid
        return p.resolve() if p.exists() else None
    for b in sorted(Path("/sys/block").glob("sd*")):
        disk = Path("/dev") / b.name
        props = _props(disk)
        if props.get("ID_VENDOR_ID") != vendor or \
                (product and props.get("ID_MODEL_ID") != product):
            continue
        parts = sorted(b.glob(f"{b.name}[0-9]*"))
        return Path("/dev") / parts[0].name if parts else disk
    return None


def wait(uuid: str | None = None, timeout: float = 120,
         alive: Callable[[], bool] | None = None, product: str | None = None) -> Path:
    """Until the card shows up; ALIVE (if given) false stops the wait early"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dev = find(uuid, product)
        if dev:
            time.sleep(1)               # let udisks see it too
            return dev
        if alive and not alive():
            break
        time.sleep(0.5)
    raise CardError("the card did not appear over USB")


def _udisks(verb: str, dev: Path) -> str:
    return subprocess.run(["udisksctl", verb, "-b", str(dev)], capture_output=True,
                          text=True, check=True).stdout


def mount(dev: Path, tries: int = 20) -> Path:
    for i in range(tries):
        try:
            m = re.search(r" at (.+?)\.?$", _udisks("mount", dev).strip())
            if m:
                return Path(m.group(1))
        except subprocess.CalledProcessError as e:
            if "AlreadyMounted" in (e.stderr or ""):
                m = re.search(r"mounted at [`'](.+?)'", e.stderr)
                if m:
                    return Path(m.group(1))
            if i == tries - 1:
                raise CardError(f"mount {dev}: {(e.stderr or '').strip()}") from e
        time.sleep(2)
    raise CardError(f"mount {dev} failed")


def unmount(dev: Path, tries: int = 10) -> None:
    """Desktop indexers can hold files open for a while: retries"""
    subprocess.run(["sync"], check=False)
    for i in range(tries):
        try:
            _udisks("unmount", dev)
            return
        except subprocess.CalledProcessError as e:
            if "NotMounted" in (e.stderr or ""):
                return
            if i == tries - 1:
                raise CardError(f"unmount {dev}: {(e.stderr or '').strip()}") from e
        time.sleep(5)


@dataclass(frozen=True)
class Check:
    clean: bool
    report: str = ""        # fsck.fat's output; empty from udisks
    repaired: str = ""      # what a repair pass said it fixed

    def describe(self) -> str:
        if self.clean and not self.repaired:
            return "fsck: clean"
        if self.clean:
            return f"fsck: clean after the repair:\n{self.repaired}"
        if not self.report:
            return ("fsck: ERRORS (udisks' check; details and repair need the "
                    "uaccess udev rule for the card's disk, see card.py)")
        return f"fsck: ERRORS\n{self.report}"


def _fsck_fat(dev: Path, mode: str) -> tuple[bool, str]:
    """fsck.fat -n (read-only) or -a (repair): clean, and its report"""
    r = subprocess.run(["fsck.fat", mode, str(dev)], capture_output=True, text=True,
                       check=False)
    out = (r.stdout + r.stderr).strip()
    if r.returncode not in (0, 1):
        raise CardError(f"fsck.fat {mode} {dev}: {out}")
    return r.returncode == 0, out


def _udisks_check(dev: Path) -> bool:
    obj = "/org/freedesktop/UDisks2/block_devices/" + dev.name
    r = subprocess.run(["gdbus", "call", "--timeout", "900", "--system", "--dest",
                        "org.freedesktop.UDisks2", "--object-path", obj, "--method",
                        "org.freedesktop.UDisks2.Filesystem.Check", "{}"],
                       capture_output=True, text=True, check=False)
    reply = r.stdout.strip()
    if reply not in ("(true,)", "(false,)"):
        raise CardError(f"fsck {dev}: {(r.stderr or reply).strip()}")
    return reply == "(true,)"


def check(dev: Path, repair: bool = False) -> Check:
    """The unmounted card's filesystem check. REPAIR: fsck.fat -a first (it
    fixes what it can without asking), then the read-only check decides."""
    if not os.access(dev, os.R_OK | os.W_OK):
        if repair:
            raise CardError(f"repairing {dev} needs write access to it: the uaccess "
                            "udev rule for the card's disk (see card.py)")
        return Check(_udisks_check(dev))
    repaired = ""
    if repair:
        clean, out = _fsck_fat(dev, "-a")
        repaired = "" if clean else out
    clean, out = _fsck_fat(dev, "-n")
    return Check(clean, "" if clean else out, repaired)


@contextmanager
def mounted(dev: Path) -> Generator[Path]:
    mp = mount(dev)
    try:
        yield mp
    finally:
        subprocess.run(["sync", "-f", str(mp)], check=False)
        unmount(dev)
