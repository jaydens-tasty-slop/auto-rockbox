"""FPU use: a hand-made disassembly, and a real plugin ELF of the port's YP3
builds (where plugin_fpu.h turns the FPU on) against yp3dev/fpucheck.py,
whose walk followed Python's set order: the chains it prints may differ, so
only the functions are compared."""

from __future__ import annotations

import contextlib
import importlib
import io
import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

from auto_rockbox import fpucheck
from auto_rockbox.workspace import Workspace
from tests import Skip

PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
YP3DEV = PORT / "yp3dev"
PLUGIN = "apps/plugins/pokitto/pokitto_fireworks.elf"
ROOTS = ["audio_get_more", "plugin_start", "no_such_function"]

DISASSEMBLY = """
x.elf:     file format elf32-littlearm


Disassembly of section .text:

00000000 <cb>:
   0:\tpush\t{r4, lr}
   2:\tbl\t10 <helper>
   6:\tblx\tr3
   8:\tpop\t{r4, pc}

00000010 <helper>:
  10:\tb.w\t20 <floaty>
  14:\tbx\tlr

00000020 <floaty>:
  20:\tvadd.f32\ts0, s0, s1
  24:\tvmov\tr0, s0
  28:\tbx\tlr

00000030 <enable>:
  30:\tldr\tr2, [r3, #0]
  32:\torr.w\tr2, r2, #15728640\t; 0xf00000
  36:\tvpush\t{d8}
  3a:\tbx\tlr

00000040 <clean>:
  40:\tbeq.n\t44 <clean+0x4>
  42:\tbic.w\tr0, r0, #1
  44:\tbfi\tr0, r1, #0, #8
  48:\tbx\tlr
"""


def test_parse() -> None:
    funcs = fpucheck.parse(DISASSEMBLY)
    assert list(funcs) == ["cb", "helper", "floaty", "enable", "clean"]
    assert list(funcs["cb"].calls) == ["helper"] and funcs["cb"].indirect == ["blx r3"]
    assert list(funcs["helper"].calls) == ["floaty"] and funcs["helper"].indirect == []
    assert funcs["floaty"].vfp == ["vadd.f32", "vmov"]
    assert funcs["enable"].cpacr and funcs["enable"].vfp == ["vpush"]
    assert list(funcs["clean"].calls) == ["clean"] and not funcs["clean"].cpacr
    assert fpucheck.vfp_users(funcs) == ["enable: 1 VFP instructions",
                                         "floaty: 2 VFP instructions"]


def test_parse_eight_digit_addresses() -> None:
    """TS17 plugins run at 0x18380000: objdump pads none of those lines, and
    the parser once read no instruction there, so every check passed"""
    funcs = fpucheck.parse(
        "18380020 <sinf>:\n"
        "18380020:\tvldr\ts13, [pc, #188]\t; 183800e0 <sinf+0xc0>\n"
        "18380024:\tbl\t18380104 <io_read>\n"
        "\n"
        "18380104 <io_read>:\n"
        "18380104:\tbx\tlr\n")
    assert funcs["sinf"].vfp == ["vldr"] and list(funcs["sinf"].calls) == ["io_read"]
    assert fpucheck.check(funcs, ["io_read"]).failures == 0
    assert fpucheck.check(funcs, ["sinf"]).failures == 1


def test_check() -> None:
    r = fpucheck.check(fpucheck.parse(DISASSEMBLY), ["cb", "clean", "missing"])
    assert r.lines == [
        "FPU switched in a function with FPU code: enable (vpush)",
        "indirect call (unchecked): cb: blx r3",
        "FPU in interrupt path: floaty <- helper <- cb (vadd.f32)",
        "cb: 3 functions checked",
        "clean: 1 functions checked",
        "missing: not found",
    ], r.lines
    assert (r.failures, r.status) == (3, 1)
    assert fpucheck.check(fpucheck.parse(DISASSEMBLY), ["clean"]).failures == 1   # enable


def _functions_only(lines: list[str]) -> list[str]:
    """LINES with each call chain cut to the function it reached"""
    return sorted(re.sub(r"^(FPU in interrupt path|indirect call \(unchecked\)): (\S+?)"
                         r"(?: <- [^:(]*)?( \(|:)", r"\1: \2\3", x) for x in lines)


def test_real_plugin() -> None:
    elfs = [p for p in PORT.glob(f"build/yp3*/{PLUGIN}") if "sim" not in p.parts[-5]]
    elf = max(elfs, key=lambda p: p.stat().st_mtime, default=None)
    if elf is None or not (YP3DEV / "fpucheck.py").exists():
        raise Skip(f"no YP3 build of {PLUGIN} in the port's build/, or no yp3dev/fpucheck.py")
    objdump = Workspace.find(PORT).tool("objdump")
    if not Path(objdump).exists():
        raise Skip(f"no {objdump}")
    if str(YP3DEV) not in sys.path:
        sys.path.insert(0, str(YP3DEV))
    with patch.object(sys, "dont_write_bytecode", True):      # nothing written there
        old: Any = importlib.import_module("fpucheck")
    funcs = fpucheck.parse(fpucheck.disassemble(elf, objdump))
    assert {n: {"vfp": f.vfp, "calls": set(f.calls), "indirect": f.indirect, "cpacr": f.cpacr}
            for n, f in funcs.items()} == old.parse(str(elf))
    r = fpucheck.check(funcs, ROOTS)
    assert any(x.startswith("FPU in interrupt path: ") for x in r.lines), r.lines
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = old.main([str(elf), *ROOTS])
    assert (r.status, _functions_only(r.lines)) == \
        (rc, _functions_only(out.getvalue().splitlines()))
