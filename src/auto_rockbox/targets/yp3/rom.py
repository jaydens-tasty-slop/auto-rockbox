"""The boot ROM's USB recovery mode (301a:2800) and the service payload run in it.

The ROM itself only writes and reads SRAM and calls into it (smtlink_dump's
write_mem, read_mem and exec_ret). Everything else (flash, PMU registers,
resets) is done by a small Thumb payload (payload/svc.c) that is loaded again
for every call, so the host keeps no state on the device and a call that dies
leaves nothing behind. The ROM's own flash reader must never be used: in both
recovery states the SPI controller is unclocked and it waits forever; the
payload sets the controller up itself.

Flash writes outside WRITABLE are refused here and again by the payload.
"""

from __future__ import annotations

import binascii
import hashlib
import os
import struct
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from functools import cached_property
from pathlib import Path

from ...errors import ArbError
from ...workspace import ConfigError

ROM_VID, ROM_PID = "301a", "2800"   # the boot ROM's USB recovery device
SMTLINK_CHIP = "6801"               # smtlink_dump's chip selector for the SL6801

PAYLOAD_LOAD = 0x806000             # payload code; ROM leaves 0x806000..0x808000 unused
PAYLOAD_ENTRY = PAYLOAD_LOAD | 1    # Thumb entry, run by exec_ret
ARGS = 0x806C00                     # struct svc_args {magic, op, a0, a1, a2}
RESULT = 0x806C40                   # the 16-word reply, mirrored from the ROM's reply buffer
STAGE = 0x807000                    # 4 KiB: sector data in, flash data and CRCs out
REPLY_BYTES = 64
SVC_MAGIC = 0x56533359              # "Y3SV": marks a request in ARGS
REPLY_MAGIC = 0x50523359            # "Y3RP": marks a reply in RESULT

SECTOR = 4096                       # NOR erase sector, the unit of writes and CRCs
IMAGE_SIZE = 2 * 1024 * 1024        # the whole NOR
WRITABLE = (0xE000, 0x1FC000)       # below: bootloader + partition table; above: settings
CRC_BATCH = 256                     # sectors per flash_crc call (their CRCs in STAGE)
READ_CHUNK = 64                     # flash_read lengths are multiples of this

PMU_REASON = 0x23                   # bits 7:6: the recovery reason the boot path checks
PMU_WAKE = 0x00                     # bit 0: recovery armed, with PMU_REASON
PMU_RESET = 0x20                    # bit 7: the reset request the payload's resets set

PAYLOAD_SOURCES = ("svc.c", "entry.S", "svc.ld")
PAYLOAD_CFLAGS = ("--target=arm-none-eabi", "-mcpu=cortex-m4", "-mthumb", "-Os",
                  "-ffreestanding", "-fno-builtin", "-nostdlib", "-Wall", "-Wextra")


class Op(IntEnum):
    INFO = 0            # reply: caller MSP, IPSR, PRIMASK, CONTROL, VTOR, CPUID, ...
    READ32 = 1          # a0 = address
    WRITE32 = 2         # a0 = address, a1 = value; reply: read back
    PMU_READ = 3        # a0 = PMU index
    PMU_WRITE = 4       # a0 = PMU index, a1 = value (verified by reading back)
    BOOT = 5            # clear the recovery reason, PMU reset: the bootloader runs the NOR image
    RESET_TO_ROM = 6    # arm the recovery reason, PMU reset: back to ROM USB
    FLASH_CRC = 7       # a0 = first sector address, a1 = count: CRC-32 per sector to STAGE
    FLASH_SECTOR = 8    # a0 = sector address, a1 = CRC-32 of STAGE: erase + program + verify
    FLASH_READ = 9      # a0 = address, a1 = length: to STAGE


class Status(IntEnum):
    OK = 0
    BAD_ARGS = 1
    SPI_BUSY = 2
    SPI_TIMEOUT = 3
    FLASH_STATE = 4     # status registers not in the validated idle state
    WEL = 5             # write enable did not latch
    WIP_TIMEOUT = 6
    VERIFY = 7
    STAGE_CRC = 8       # STAGE does not match the CRC the host sent with it
    PMU = 9
    PMU_FLAGS = 10      # PMU reset flags not as expected: no reset done
    UNCHANGED = 11      # the sector already held the data


INFO_NAMES = ("msp", "ipsr", "primask", "control", "vtor", "cpuid", "desc", "timer",
              "timer_hi", "spi_c", "spi_10", "spi_20", "wdt_00")


class DeviceError(ArbError):
    """The player did not answer as expected in ROM USB"""


@dataclass(frozen=True)
class UsbId:
    """One USB device as sysfs names it; a re-enumeration gets a new devnum"""

    name: str
    bus: int
    dev: int

    @property
    def node(self) -> Path:
        return Path(f"/dev/bus/usb/{self.bus:03d}/{self.dev:03d}")


def rom_devices() -> list[UsbId]:
    found = []
    for d in Path("/sys/bus/usb/devices").iterdir():
        try:
            if ((d / "idVendor").read_text().strip(), (d / "idProduct").read_text().strip()) \
                    == (ROM_VID, ROM_PID):
                found.append(UsbId(d.name, int((d / "busnum").read_text()),
                                   int((d / "devnum").read_text())))
        except OSError:
            continue
    return found


def wait_rom(deadline: float, previous: UsbId | None = None,
             tick: Callable[[], None] | None = None) -> UsbId | None:
    """Until exactly one ROM device (other than PREVIOUS) is present, accessible
    and stable for three polls: a fresh device can vanish again while the ROM
    re-enumerates, and udev grants access a moment after it appears"""
    stable, last = 0, None
    while time.monotonic() < deadline:
        if tick:
            tick()
        nodes = rom_devices()
        cur = nodes[0] if len(nodes) == 1 else None
        if cur and cur != previous:
            ok = cur == last and os.access(cur.node, os.R_OK | os.W_OK)
            stable = stable + 1 if ok else 0
            if stable >= 3:
                return cur
        else:
            stable = 0
        last = cur
        time.sleep(0.1)
    return None


def build_payload(out: Path, llvm: str = "") -> Path:
    """The payload binary in OUT, rebuilt when its sources or flags change.
    LLVM is the prefix of clang, ld.lld and llvm-objcopy ("" for PATH)."""
    src = Path(__file__).with_name("payload")
    digest = hashlib.sha256(repr((PAYLOAD_CFLAGS, llvm)).encode())
    for name in PAYLOAD_SOURCES:
        digest.update((src / name).read_bytes())
    binary, stamp = out / "svc.bin", out / "svc.stamp"
    if binary.exists() and stamp.exists() and stamp.read_text() == digest.hexdigest():
        return binary
    out.mkdir(parents=True, exist_ok=True)
    steps = [
        [f"{llvm}clang", *PAYLOAD_CFLAGS, "-c", str(src / "svc.c"), "-o", str(out / "svc.o")],
        [f"{llvm}clang", *PAYLOAD_CFLAGS, "-c", str(src / "entry.S"), "-o", str(out / "entry.o")],
        [f"{llvm}ld.lld", "-T", str(src / "svc.ld"), "--gc-sections", str(out / "entry.o"),
         str(out / "svc.o"), "-o", str(out / "svc.elf")],
        [f"{llvm}llvm-objcopy", "-O", "binary", "--only-section=.text", str(out / "svc.elf"),
         str(binary)],
    ]
    for argv in steps:
        try:
            subprocess.run(argv, check=True, capture_output=True, text=True)
        except FileNotFoundError as e:
            raise ConfigError(f"[targets.yp3] llvm: {argv[0]} not found (the payload needs "
                              "clang, ld.lld and llvm-objcopy)") from e
        except subprocess.CalledProcessError as e:
            raise DeviceError(f"payload build failed: {e.stderr.strip()}") from e
    stamp.write_text(digest.hexdigest())
    return binary


@dataclass(frozen=True)
class Reply:
    status: Status | int
    words: tuple[int, ...]          # the 16 reply words; results from words[3]

    @property
    def value(self) -> int:
        return self.words[3]


def sector_crc(data: bytes) -> int:
    return binascii.crc32(data) & 0xFFFFFFFF


class Rom:
    """Payload calls through smtlink_dump. Not locked: callers hold the
    target's device lock around a whole conversation."""

    def __init__(self, smtlink: Path, payload: Callable[[], Path]) -> None:
        self.tool = smtlink
        self._build = payload
        self._tmp = tempfile.TemporaryDirectory(prefix="arb-yp3-")
        self.tmp = Path(self._tmp.name)

    @cached_property
    def payload(self) -> Path:
        return self._build()

    def smtlink(self, *cmds: str | int | Path, timeout: float = 10.0,
                usb_timeout_ms: int | None = None) -> str:
        argv = [str(self.tool), "--id", f"{ROM_VID}:{ROM_PID}", "--wait", "1",
                "chip", SMTLINK_CHIP]
        if usb_timeout_ms:
            argv += ["timeout", str(usb_timeout_ms)]
        argv += [str(c) for c in cmds]
        try:
            p = subprocess.run(argv, capture_output=True, timeout=timeout, text=True,
                               check=False)
        except subprocess.TimeoutExpired as e:
            raise DeviceError(f"smtlink timeout: {' '.join(map(str, cmds))[:120]}") from e
        if p.returncode:
            raise DeviceError(f"smtlink rc={p.returncode}: {p.stderr.strip()[-300:]}")
        return p.stderr

    def call(self, op: Op, a0: int = 0, a1: int = 0, a2: int = 0, *, stage: bytes | None = None,
             usb_timeout_ms: int | None = None) -> Reply:
        """Load the payload (and STAGE), run one op, read its reply"""
        args = self.tmp / "args.bin"
        # Zeros past the request clear RESULT, so a stale reply never passes
        args.write_bytes(struct.pack("<5I", SVC_MAGIC, op, a0, a1, a2)
                         + bytes(RESULT - ARGS - 20 + REPLY_BYTES))
        out = self.tmp / "result.bin"
        out.unlink(missing_ok=True)
        cmds: list[str | int | Path] = ["write_mem", hex(PAYLOAD_LOAD), 0, 0, self.payload,
                                        "write_mem", hex(ARGS), 0, 0, args]
        if stage is not None:
            sf = self.tmp / "stage.bin"
            sf.write_bytes(stage)
            cmds += ["write_mem", hex(STAGE), 0, 0, sf]
        cmds += ["exec_ret", hex(PAYLOAD_ENTRY), REPLY_BYTES,
                 "read_mem", hex(RESULT), REPLY_BYTES, out]
        self.smtlink(*cmds, usb_timeout_ms=usb_timeout_ms)
        raw = out.read_bytes() if out.exists() else b""
        if len(raw) != REPLY_BYTES:
            raise DeviceError(f"{op.name.lower()}: no reply")
        w = struct.unpack("<16I", raw)
        if w[0] != REPLY_MAGIC or w[1] != op:
            raise DeviceError(f"{op.name.lower()}: bad reply {raw[:16].hex()}")
        try:
            return Reply(Status(w[2]), w)
        except ValueError:
            return Reply(w[2], w)

    def ok(self, op: Op, a0: int = 0, a1: int = 0, a2: int = 0, *,
           usb_timeout_ms: int | None = None) -> Reply:
        r = self.call(op, a0, a1, a2, usb_timeout_ms=usb_timeout_ms)
        if r.status != Status.OK:
            raise DeviceError(f"{op.name.lower()} {a0:#x} {a1:#x}: {_status(r.status)} "
                              f"{[hex(x) for x in r.words[3:6]]}")
        return r

    def info(self) -> dict[str, int]:
        return dict(zip(INFO_NAMES, self.ok(Op.INFO).words[3:], strict=True))

    def read32(self, addr: int) -> int:
        return self.ok(Op.READ32, addr).value

    def write32(self, addr: int, value: int) -> int:
        return self.ok(Op.WRITE32, addr, value).value

    def pmu_read(self, index: int) -> int:
        return self.ok(Op.PMU_READ, index).value

    def pmu_write(self, index: int, value: int) -> None:
        self.ok(Op.PMU_WRITE, index, value & 0xFF)

    def read_ram(self, addr: int, n: int) -> bytes:
        """SRAM through the ROM's own reader (no payload)"""
        out = self.tmp / "ram.bin"
        out.unlink(missing_ok=True)
        self.smtlink("read_mem", hex(addr), n, out)
        return out.read_bytes()

    def read_flash(self, addr: int, n: int) -> bytes:
        if n % READ_CHUNK or addr + n > IMAGE_SIZE:
            raise ValueError(f"flash read {addr:#x}+{n:#x}: a multiple of {READ_CHUNK} "
                             "bytes inside the flash is needed")
        data = b""
        for off in range(0, n, SECTOR):
            k = min(SECTOR, n - off)
            self.ok(Op.FLASH_READ, addr + off, k)
            data += self.read_ram(STAGE, k)
        return data

    def flash_crcs(self) -> list[int]:
        crcs: list[int] = []
        sectors = IMAGE_SIZE // SECTOR
        for s in range(0, sectors, CRC_BATCH):
            n = min(CRC_BATCH, sectors - s)
            self.ok(Op.FLASH_CRC, s * SECTOR, n, usb_timeout_ms=5000)
            crcs += struct.unpack(f"<{n}I", self.read_ram(STAGE, n * 4))
        return crcs

    def write_sector(self, addr: int, data: bytes) -> Status | int:
        if addr % SECTOR or len(data) != SECTOR or not WRITABLE[0] <= addr < WRITABLE[1]:
            raise ValueError(f"sector {addr:#x}: outside the writable flash "
                             f"[{WRITABLE[0]:#x}, {WRITABLE[1]:#x})")
        return self.call(Op.FLASH_SECTOR, addr, sector_crc(data), stage=data,
                         usb_timeout_ms=5000).status

    def flash(self, image: bytes, echo: Callable[[str], None] = print) -> None:
        """Writes the sectors of IMAGE that differ from the flash, inside
        WRITABLE only, then checks them all again"""
        if len(image) != IMAGE_SIZE:
            raise ValueError(f"a flash image is {IMAGE_SIZE} bytes, not {len(image)}")
        t0 = time.monotonic()
        want = [sector_crc(image[i:i + SECTOR]) for i in range(0, IMAGE_SIZE, SECTOR)]
        diff = [i for i, (w, h) in enumerate(zip(want, self.flash_crcs(), strict=True))
                if w != h]
        protected = [hex(i * SECTOR) for i in diff if not _writable(i)]
        if protected:
            echo(f"warning: image differs in protected sectors (left untouched): {protected}")
        diff = [i for i in diff if _writable(i)]
        echo(f"{len(diff)} sectors to write")
        for n, i in enumerate(diff):
            st = self.write_sector(i * SECTOR, image[i * SECTOR:(i + 1) * SECTOR])
            if st not in (Status.OK, Status.UNCHANGED):
                raise DeviceError(f"sector {i * SECTOR:#x}: {_status(st)}")
            if n % 16 == 15:
                echo(f"  {n + 1}/{len(diff)}")
        after = self.flash_crcs()
        still = [hex(i * SECTOR) for i in range(len(want))
                 if want[i] != after[i] and _writable(i)]
        if still:
            raise DeviceError(f"verify failed: {still}")
        echo(f"flash verified ({time.monotonic() - t0:.1f} s)")

    def reset(self, op: Op) -> UsbId | None:
        """BOOT or RESET_TO_ROM. The player drops off USB, so no reply is the
        good outcome; returns the ROM device it was, to wait for a new one."""
        if op not in (Op.BOOT, Op.RESET_TO_ROM):
            raise ValueError(f"{op.name}: not a reset")
        before = rom_devices()
        try:
            r = self.call(op, usb_timeout_ms=3000)
        except DeviceError:
            return before[0] if before else None
        raise DeviceError(f"{op.name.lower()} returned {_status(r.status)} "
                          f"pmu23/00/20={[hex(x) for x in r.words[3:6]]}")


def _writable(sector: int) -> bool:
    return WRITABLE[0] <= sector * SECTOR < WRITABLE[1]


def _status(st: Status | int) -> str:
    return st.name.lower() if isinstance(st, Status) else f"status {st}"
