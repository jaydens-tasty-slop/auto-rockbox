"""auto_rockbox.treecmp: the classes of a difference between two tree builds,
and make's failures"""

from __future__ import annotations

import tempfile
from pathlib import Path

from auto_rockbox import treecmp

ID_A, ID_B = (0x0D7FED2B).to_bytes(4, "little"), (0x11223344).to_bytes(4, "little")


def elf(body: str, debug: str = "") -> bytes:
    """A stand-in ELF: the magic, its sections' text, its debug info's"""
    return treecmp.ELF_MAGIC + f"{body}|{debug}".encode()


def dump(data: bytes) -> str:
    """The stand-in's sections without debug info, as the real dump"""
    return data[len(treecmp.ELF_MAGIC):].decode().split("|")[0]


def module(header_id: bytes, body: bytes = b"code") -> bytes:
    return b"\0" * 16 + header_id + body


def test_classify() -> None:
    ids = (ID_A, ID_B)
    c = treecmp.classify
    assert c(module(ID_A), module(ID_A), ids, dump) == treecmp.SAME
    assert c(module(ID_A), module(ID_B), ids, dump) == treecmp.MODULE
    assert c(module(ID_A, b"x" + ID_A), module(ID_B, b"x" + ID_B), ids, dump) == treecmp.MODULE
    assert c(module(ID_A), module(ID_B), None, dump) == treecmp.DIFFERS
    assert c(module(ID_A, b"code2"), module(ID_B), ids, dump) == treecmp.DIFFERS
    assert c(b"Exe: 09:19:50 Nov 19 2026", b"Exe: 09:21:00 Nov 19 2026", None,
             dump) == treecmp.STAMP
    assert c(b"Exe: 09:19:50 Nov 19 2026", b"Exe: 09:21:00 Nov 19 2027", None,
             dump) == treecmp.STAMP
    assert c(elf("code", "line 10"), elf("code", "line 12"), None, dump) == treecmp.CODE
    assert c(elf("R_ARM_ABS32 CSWTCH.25"), elf("R_ARM_ABS32 CSWTCH.26"), None,
             dump) == treecmp.CODE
    assert c(elf("ldr r0, AAAA"), elf("ldr r0, BBBB"), (b"AAAA", b"BBBB"),
             dump) == treecmp.MODULE     # the core's literal of the ID
    assert c(elf("code"), elf("code2"), None, dump) == treecmp.DIFFERS


def test_failures() -> None:
    work = Path("/build/arb/tree")
    log = ("make: *** [/build/arb/tree/apps/plugins/x/p.o] Error 1\n"
           "make: *** [../tree-src/tools/root.make:280: /build/arb/tree/rockbox.elf] Error 1\n"
           "make: *** [/build/arb/tree/apps/plugins/x/p.o] Error 1\n")
    assert treecmp.failures(log, work) == ["apps/plugins/x/p.o", "rockbox.elf"]


def test_compare() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp, "a"), Path(tmp, "b")
        files = {
            "apps/same.o": (elf("x"), elf("x")),
            "apps/debug.o": (elf("x", "1"), elf("x", "2")),
            "apps/plugins/p.rock": (module(ID_A), module(ID_B)),
            "apps/changed.o": (elf("x"), elf("y")),
        }
        for name, (da, db) in files.items():
            for d, data in ((a, da), (b, db)):
                (d / name).parent.mkdir(parents=True, exist_ok=True)
                (d / name).write_bytes(data)
        (a / "apps/gone.o").write_bytes(elf("z"))
        for d, i, failed in ((a, 0x0D7FED2B, "rockbox.elf\n"),
                             (b, 0x11223344, "rockbox.elf\nq.o\n")):
            (d / treecmp.ID_FILE).write_text(f"module_build_id = {i:#010x};\n")
            (d / treecmp.FAILED).write_text(failed)
        c = treecmp.compare(a, b, dump)
        assert c.classes == {treecmp.SAME: ["apps/same.o"], treecmp.CODE: ["apps/debug.o"],
                             treecmp.MODULE: ["apps/plugins/p.rock"],
                             treecmp.DIFFERS: ["apps/changed.o"]}
        assert c.only_a == ["apps/gone.o"] and c.failed_b == ["q.o"] and c.status == 1
        assert c.lines()[-1] == ("4 outputs: 1 identical, 1 same code and data (debug info, "
                                 "numbered names aside), 1 module build ID only, 1 DIFFERS")
