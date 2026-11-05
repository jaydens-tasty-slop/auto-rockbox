"""Code on the player's CPU from download mode.

The ROM's switch command does not run its target once: it registers it as a
hook its USB loop calls continuously (~750k calls/s). So switch points once
at a resident stub (gadgets/hook.S) that calls a gadget only when the host
arms it, and gadgets are loaded while the stub is idle. Never point switch at
a gadget, and never change code at a registered address: the ROM runs it
half-written.

The one gadget is gadgets/ops.c: a list of register reads, writes, polls,
delays, copies and calls (class Ops) run in one hook call with IRQs masked
and a fault table that logs and skips a faulting access instead of wedging
the ROM. Its sources ship in this package and are built on demand with the
workspace's cross toolchain into <build>/arb/ts17.

RAM it uses: gadget 0x01018000, op list 0x01018800-0x0101980f, hook control
0x01019980, hook 0x010199c0, CALL arguments 0x01019c00, fault vectors
0x0101a000, output from 0x01040000 (where Rockbox's framebuffer is too).
"""

from __future__ import annotations

import hashlib
import importlib.resources
import struct
import subprocess
import sys
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Self

from ...workspace import Workspace
from .adfu import Adfu, Ts17Error, present

HOOK_CODE = 0x010199C0          # resident stub (hook.S, position independent)
HOOK_CTL = 0x01019980           # ARM, FN, DONE words (hook.S)
HOOK_ARM = 0xA4A4C0DE           # written last: the stub clears it, then calls FN
HOOK_DONE = 0xD0D0D0D0          # the stub's mark that FN returned
GADGET_CODE = 0x01018000        # gadget entry (gadget.ld)
GADGET_ARGS = 0x01018800        # ops.c: n_ops, current op, then the ops from +0x10
GADGET_OUT = 0x01040000         # ops.c: status words, then results
CALL_ARGS = 0x01019C00          # ops.c: r0..r3 for each CALL op, 16 bytes each
FAULT_TABLE = 0x0101A000        # ops.c: the RAM vector table it installs
MAX_OPS = 256
MAX_CALLS = 64
STATUS_WORDS = 16               # ops.c O_RES: results follow the status block
FAULT_OPS_SHOWN = 8             # ops.c O_FAULT_OPS: indices of the first faults
CALL_TIMEOUT_S = 30.0
LEAVE_S = 3.0                   # a launch or reset takes the device off the bus by then

CFLAGS = ["-mcpu=cortex-m4", "-mthumb"]
GADGET_CFLAGS = [*CFLAGS, "-Os", "-ffreestanding", "-nostdlib", "-fno-builtin", "-Wall"]
SOURCES = ("hook.S", "ops.c", "gadget.ld")


class Gadgets:
    """The hook stub and the ops gadget as flat binaries, rebuilt when the
    sources, the tools or the flags change (a key file beside each binary)"""

    def __init__(self, ws: Workspace) -> None:
        self.gcc, self.objcopy = ws.tool("gcc"), ws.tool("objcopy")
        self.cache = ws.build_dir / "arb" / "ts17"

    def hook(self) -> bytes:
        return self._get("hook")

    def ops(self) -> bytes:
        return self._get("ops")

    def _steps(self, name: str, src: Path) -> list[list[str]]:
        out = str(self.cache / name)
        if name == "hook":
            return [[self.gcc, *CFLAGS, "-c", str(src / "hook.S"), "-o", f"{out}.o"],
                    [self.objcopy, "-O", "binary", "-j", ".text", f"{out}.o", f"{out}.bin"]]
        return [[self.gcc, *GADGET_CFLAGS, "-T", str(src / "gadget.ld"), str(src / "ops.c"),
                 "-o", f"{out}.elf"],
                [self.objcopy, "-O", "binary", f"{out}.elf", f"{out}.bin"]]

    def _get(self, name: str) -> bytes:
        out, stamp = self.cache / f"{name}.bin", self.cache / f"{name}.key"
        with importlib.resources.as_file(importlib.resources.files(__package__) / "gadgets") as src:
            key = hashlib.sha256("\0".join([self.gcc, self.objcopy, *GADGET_CFLAGS]).encode())
            for f in SOURCES:
                key.update(b"\0" + (src / f).read_bytes())
            want = key.hexdigest()
            if not (out.exists() and stamp.exists() and stamp.read_text() == want):
                self.cache.mkdir(parents=True, exist_ok=True)
                for cmd in self._steps(name, src):
                    run_tool(cmd)
                stamp.write_text(want)
        return out.read_bytes()


def run_tool(cmd: list[str]) -> None:
    """A cross-toolchain command; its errors name the config key that picks it"""
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except FileNotFoundError as e:
        raise Ts17Error(f"{cmd[0]} not found: set [paths] toolchain in auto-rockbox.toml") from e
    except subprocess.CalledProcessError as e:
        raise Ts17Error(f"{' '.join(cmd)} failed:\n{e.stderr}") from e


class Op(IntEnum):
    """ops.c's op kinds; an op is (kind, addr, val, mask)"""

    END = 0
    W32 = 1         # *addr = val
    R32 = 2         # result <- *addr
    W8 = 3
    R8 = 4
    POLL = 5        # until (*addr & mask) == val; result <- spins, ~0 on timeout
    DELAY = 6       # spin val iterations
    RMW = 7         # *addr = (*addr & ~mask) | val
    RN32 = 8
    RN8 = 9
    COPY = 10       # mask words from addr to val
    CALL = 11       # result <- addr(r0..r3 from the four words at val)


class Ops:
    """An op list for one gadget call: a register sequence that runs without
    USB round trips in between"""

    def __init__(self) -> None:
        self.ops: list[tuple[int, int, int, int]] = []
        self.call_args: list[tuple[int, ...]] = []

    def _add(self, kind: Op, addr: int = 0, val: int = 0, mask: int = 0) -> Self:
        if len(self.ops) >= MAX_OPS:
            raise Ts17Error(f"op list over {MAX_OPS} ops")
        self.ops.append((kind, addr, val, mask))
        return self

    def w32(self, addr: int, val: int) -> Self:
        return self._add(Op.W32, addr, val)

    def r32(self, addr: int) -> Self:
        return self._add(Op.R32, addr)

    def poll(self, addr: int, mask: int, val: int) -> Self:
        return self._add(Op.POLL, addr, val, mask)

    def delay(self, spins: int) -> Self:
        return self._add(Op.DELAY, 0, spins)

    def rmw(self, addr: int, mask: int, val: int) -> Self:
        return self._add(Op.RMW, addr, val, mask)

    def copy(self, src: int, dst: int, nbytes: int) -> Self:
        return self._add(Op.COPY, src, dst, nbytes // 4)

    def call(self, fn: int, *args: int) -> Self:
        """FN(r0..r3) on the player's CPU (Thumb bit added); its return value
        is one result word"""
        if len(self.call_args) >= MAX_CALLS or len(args) > 4:
            raise Ts17Error(f"at most {MAX_CALLS} calls of up to 4 arguments")
        slot = CALL_ARGS + 16 * len(self.call_args)
        self.call_args.append((*args, 0, 0, 0, 0)[:4])
        return self._add(Op.CALL, fn, slot)

    def blobs(self) -> tuple[bytes, bytes]:
        """The op block for GADGET_ARGS and the argument block for CALL_ARGS"""
        ops = struct.pack("<I12x", len(self.ops)) + b"".join(struct.pack("<4I", *o)
                                                              for o in self.ops)
        return ops, b"".join(struct.pack("<4I", *a) for a in self.call_args)


@dataclass(frozen=True)
class Result:
    values: list[int]
    nfault: int
    fault_ops: list[int]        # indices of the first faulting ops
    cfsr: int
    hfsr: int
    bfar: int
    vtor_set: int               # the VTOR the gadget ran with

    @classmethod
    def parse(cls, status: bytes, values: list[int]) -> Result:
        (_count, nfault, _vtor_rom, vtor_set, cfsr, hfsr, bfar, _mmfar,
         *fault_ops) = struct.unpack(f"<{STATUS_WORDS}I", status)
        return cls(values, nfault, fault_ops[:min(nfault, FAULT_OPS_SHOWN)],
                   cfsr, hfsr, bfar, vtor_set)

    def describe_faults(self) -> str:
        return (f"{self.nfault} faults, ops {self.fault_ops}; CFSR {self.cfsr:08x} "
                f"HFSR {self.hfsr:08x} BFAR {self.bfar:08x}")


class Session:
    """Download mode with the hook registered and the ops gadget loaded"""

    def __init__(self, link: Adfu, gadgets: Gadgets) -> None:
        self.link = link
        self._install_hook(gadgets.hook())
        code = gadgets.ops()
        link.write(GADGET_CODE, code)
        if link.read(GADGET_CODE, len(code)) != code:
            raise Ts17Error("the ops gadget did not read back")
        if (r := self.run(Ops())).vtor_set != FAULT_TABLE:
            raise Ts17Error(f"VTOR stayed {r.vtor_set:08x}, not the gadget's fault table "
                            f"{FAULT_TABLE:08x}: refusing to go on")

    def _install_hook(self, code: bytes) -> None:
        """Rewriting identical bytes under a registered hook is harmless; the
        stub is only written when RAM holds something else"""
        if self.link.read(HOOK_CODE, len(code)) != code:
            self.link.write(HOOK_CTL, bytes(12))
            self.link.write(HOOK_CODE, code)
            if self.link.read(HOOK_CODE, len(code)) != code:
                raise Ts17Error("the hook stub did not read back")
        self.link.switch(HOOK_CODE)

    def _send(self, ops: Ops) -> None:
        """Loads OPS and arms the hook (last) to run the gadget once"""
        blob, args = ops.blobs()
        self.link.write(GADGET_ARGS, blob)
        if args:
            self.link.write(CALL_ARGS, args)
        self.link.write(HOOK_CTL + 4, struct.pack("<II", GADGET_CODE, 0))
        self.link.write(HOOK_CTL, struct.pack("<I", HOOK_ARM))

    def run(self, ops: Ops, timeout_s: float = CALL_TIMEOUT_S) -> Result:
        self._send(ops)
        deadline = time.monotonic() + timeout_s
        with self.link.patience(timeout_s):
            while struct.unpack("<I", self.link.read(HOOK_CTL + 8, 4))[0] != HOOK_DONE:
                if time.monotonic() > deadline:
                    raise Ts17Error(f"the ops gadget did not return within {timeout_s:.0f} s")
                time.sleep(0.001)
        status = self.link.read(GADGET_OUT, 4 * STATUS_WORDS)
        n = struct.unpack_from("<I", status)[0]
        raw = self.link.read(GADGET_OUT + 4 * STATUS_WORDS, 4 * n) if n else b""
        return Result.parse(status, list(struct.unpack(f"<{n}I", raw)))

    def values(self, ops: Ops) -> list[int]:
        """OPS' results; faults (skipped by the gadget) are reported, as a
        probe of an unknown register may fault and that is its answer"""
        r = self.run(ops)
        if r.nfault:
            print(f"ts17 gadget: {r.describe_faults()}", file=sys.stderr)
        return r.values

    def hand_over(self, ops: Ops, before: tuple[str, str] | None) -> None:
        """Starts OPS that take the CPU from the ROM (a launch or a reset).
        The ROM may never answer the write that arms them; that is fine if
        the device (BEFORE: adfu.present() beforehand) has left the bus."""
        try:
            self._send(ops)
        except Ts17Error:
            end = time.monotonic() + LEAVE_S
            while present() == before:
                if time.monotonic() > end:
                    raise
                time.sleep(0.2)


@contextmanager
def connect(ws: Workspace, wait_s: float = 5.0, keep: int = 0) -> Generator[Session]:
    """A session; KEEP bytes of the gadget's output block are put back
    afterwards (Rockbox's framebuffer starts there)"""
    with Adfu(wait_s) as link:
        saved = link.read(GADGET_OUT, keep) if keep else b""
        yield Session(link, Gadgets(ws))
        if saved:
            link.write(GADGET_OUT, saved)
