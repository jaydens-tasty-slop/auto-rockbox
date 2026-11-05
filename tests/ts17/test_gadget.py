"""The ops gadget's host side, and its on-demand build."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from auto_rockbox.targets.ts17 import Ts17Error
from auto_rockbox.targets.ts17.gadget import (
    CALL_ARGS,
    GADGET_ARGS,
    GADGET_CODE,
    MAX_OPS,
    STATUS_WORDS,
    Gadgets,
    Op,
    Ops,
    Result,
)
from auto_rockbox.workspace import Workspace

from . import Skip

REPO = Path(__file__).resolve().parents[3]
TOOLCHAIN = REPO / "toolchain" / "gcc-arm-none-eabi-10.3-2021.10" / "bin" / "arm-none-eabi-"
OLD_BINS = REPO / "ts17dev" / "gadgets"


def test_op_numbers_match_ops_c() -> None:
    src = (Path(__file__).resolve().parents[2] / "src" / "auto_rockbox" / "targets" / "ts17"
           / "gadgets" / "ops.c").read_text()
    names = src[src.index("OP_END"):src.index("};", src.index("OP_END"))]
    order = [w.split(",")[0].removeprefix("OP_") for w in names.split() if w.startswith("OP_")]
    assert order == [op.name for op in Op]


def test_blobs() -> None:
    ops = Ops().w32(0x40004100, 7).call(0x4219, 0, 0x14000, 0x01048000)
    blob, args = ops.blobs()
    assert blob == (struct.pack("<I12x", 2) + struct.pack("<4I", Op.W32, 0x40004100, 7, 0)
                    + struct.pack("<4I", Op.CALL, 0x4219, CALL_ARGS, 0))
    assert args == struct.pack("<4I", 0, 0x14000, 0x01048000, 0)
    assert len(blob) <= 0x1010 and GADGET_ARGS + len(blob) <= 0x01019810


def test_op_limits() -> None:
    ops = Ops()
    for _ in range(MAX_OPS):
        ops.delay(1)
    try:
        ops.delay(1)
    except Ts17Error:
        return
    raise AssertionError("op list grew past MAX_OPS")


def test_result_parse() -> None:
    status = struct.pack(f"<{STATUS_WORDS}I", 2, 2, 0x10000000, 0x0101A000, 0x82, 0, 0x40000000,
                         0, 3, 5, *([0xEEEE] * 6))
    r = Result.parse(status, [1, 2])
    assert r.fault_ops == [3, 5] and r.nfault == 2 and r.vtor_set == 0x0101A000
    assert "BFAR 40000000" in r.describe_faults()


def test_build_on_demand() -> None:
    if not Path(f"{TOOLCHAIN}gcc").exists():
        raise Skip(f"no toolchain at {TOOLCHAIN}")
    with tempfile.TemporaryDirectory() as tmp:
        ws = Workspace(Path(tmp), {"paths": {"toolchain": str(TOOLCHAIN), "build": "b"}})
        g = Gadgets(ws)
        hook, ops = g.hook(), g.ops()
        assert g.cache == Path(tmp) / "b" / "arb" / "ts17"
        stamp = (g.cache / "ops.key").stat().st_mtime_ns
        assert g.ops() == ops and (g.cache / "ops.key").stat().st_mtime_ns == stamp
        assert 0 < len(ops) <= 0x800 and GADGET_CODE + len(ops) <= GADGET_ARGS
        assert hook[:2] and len(hook) % 2 == 0
        for name, got in (("hook", hook), ("ops", ops)):
            old = OLD_BINS / f"{name}.bin"
            if old.exists():
                assert got == old.read_bytes(), f"{name} differs from ts17dev's build"
