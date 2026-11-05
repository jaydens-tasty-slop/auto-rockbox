"""The YP3 target's pure parts, checked against yp3dev's own functions where
the port's checkout has them (auto-rockbox inside rockbox-yp3), else alone.
No device is touched.

    .venv/bin/python -m tests.yp3.test_yp3        (or python -m tests yp3)
"""

from __future__ import annotations

import binascii
import importlib
import random
import shutil
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

from auto_rockbox import registry
from auto_rockbox.elf import Elf, Symbolizer
from auto_rockbox.run import run
from auto_rockbox.shell import Reply
from auto_rockbox.targets.yp3 import Yp3, Yp3Run, image, power, prof, retained, rom, shot
from auto_rockbox.workspace import Workspace
from tests import Skip

PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
YP3DEV = PORT / "yp3dev"
ELF = PORT / "build/yp3/rockbox.elf"
TOOLCHAIN = PORT / "toolchain/gcc-arm-none-eabi-10.3-2021.10/bin/arm-none-eabi-"

NO_PORT = "no rockbox-yp3 checkout (build/yp3/rockbox.elf, toolchain) around this repo"


def _yp3dev() -> Any:
    """yp3dev/yp3.py as a module (importing it touches no device)"""
    if str(YP3DEV) not in sys.path:
        sys.path.insert(0, str(YP3DEV))
    return importlib.import_module("yp3")


def have_port() -> bool:
    return (YP3DEV / "yp3.py").exists() and ELF.exists() and Path(f"{TOOLCHAIN}nm").exists()


def names() -> retained.Names:
    sym = Symbolizer(ELF, f"{TOOLCHAIN}addr2line")
    return lambda addrs: [n.split(" ", 1)[0] for n in sym.names(addrs)]


def functions_at(n: int) -> list[int]:
    """Addresses a little way into N distinct sized functions"""
    funcs = [f for f in Elf(ELF).functions if f[1] >= 16]
    step = len(funcs) // n
    return [funcs[i * step][0] + 4 for i in range(n)]


# The image

def test_dev_config_and_bundle() -> None:
    settings = {"return": "40", "keys": "S 1500 D S", "play": "/yp3dev/tone.mp3", "usb": "1"}
    text = image.dev_config(settings)
    assert text == b"return=40\nkeys=S 1500 D S\nplay=/yp3dev/tone.mp3\nusb=1\n"
    for bad in ({"a=b": "1"}, {"k": "x\ny"}, {"": "1"}):
        try:
            image.dev_config(bad)
        except image.ImageError:
            continue
        raise AssertionError(bad)
    with tempfile.TemporaryDirectory() as tmp:
        a, b = Path(tmp) / "a", Path(tmp) / "b.bin"
        a.write_bytes(b"hello")
        b.write_bytes(bytes(range(256)) * 3 + b"xyz")
        files = [(str(a), "/x/a.txt"), (str(b), "/.rockbox/b")]
        bundle = image.sdpush_bundle((sd, Path(f).read_bytes()) for f, sd in files)
        magic, plen, dlen, crc = struct.unpack_from("<4I", bundle)
        assert (magic, plen, dlen, crc) == (image.BUNDLE_MAGIC, 8, 5,
                                            binascii.crc32(b"hello"))
        assert bundle[16:24] == b"/x/a.txt" and bundle[24:32] == b"hello\0\0\0"
        if have_port():
            want_bundle, want_text = _yp3dev().dev_areas(
                [f"{k}={v}" for k, v in settings.items()], [f"{f}={sd}" for f, sd in files])
            assert bundle == want_bundle and text == want_text
    try:
        image.sdpush_bundle([("/big", bytes(image.DEV_CONFIG - image.DEV_BUNDLE))])
    except image.ImageError:
        pass
    else:
        raise AssertionError("an oversized bundle passed")


def test_place_elf_matches_package_py() -> None:
    if not have_port():
        raise Skip(NO_PORT)
    base = random.Random(1).randbytes(rom.IMAGE_SIZE)
    with tempfile.TemporaryDirectory() as tmp:
        bp, out = Path(tmp) / "base.bin", Path(tmp) / "out.bin"
        bp.write_bytes(base)
        subprocess.run([sys.executable, str(YP3DEV / "package.py"), str(ELF), str(bp), str(out)],
                       check=True, capture_output=True)
        img, end = image.place_elf(Elf(ELF), base)
        assert bytes(img) == out.read_bytes()
        assert image.CODE_MIN < end <= image.DEV_BUNDLE
        assert img[:0xE000] == base[:0xE000] and img[0x1FC000:] == base[0x1FC000:]


def test_dev_image_matches_image_for() -> None:
    """yp3.py's image_for packages against <port>/build/base.bin; so does this"""
    base_path = PORT / "build/base.bin"
    if not have_port() or not base_path.exists():
        raise Skip(f"no port checkout, or no {base_path}")
    yp3 = _yp3dev()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        elf = t / "rockbox.elf"
        shutil.copy(ELF, elf)
        xip = t / image.XIP_CODECS
        xip.parent.mkdir(parents=True)
        xip.write_bytes(random.Random(2).randbytes(5000))
        blob = t / "blob"
        blob.write_bytes(b"\x55" * 300)
        push = t / "p.txt"
        push.write_bytes(b"pushed")
        config = {"return": "40", "ls": "/,/.rockbox"}
        want = yp3.image_for(elf, None, [f"{k}={v}" for k, v in config.items()],
                             [f"{push}=/p.txt"], False, [f"{blob}@0x120000"])
        got = image.dev_image(elf, base_path.read_bytes(), config,
                              pushes=[(push, "/p.txt")], blobs=[(blob, 0x120000)])
        assert got == Path(want).read_bytes()
        assert got[image.XIP_CODEC_AREA:image.XIP_CODEC_AREA + 5000] == xip.read_bytes()
        for spec in ("x@0x1000", "x@0x150000", "x"):
            try:
                if spec == "x":
                    image.parse_blob(spec)
                else:
                    image.dev_image(elf, base_path.read_bytes(), config,
                                    blobs=[(blob, image.parse_blob(spec)[1])])
            except image.ImageError:
                continue
            raise AssertionError(f"blob {spec} passed")


def test_run_config() -> None:
    req = Yp3Run(Path("x.elf"), 40, "S D", {"usb": "storage", "speaker": "0"}, usb=True)
    assert Yp3.run_config(req) == {"return": "40", "keys": "S D", "usb": "storage",
                                   "speaker": "0"}
    assert Yp3.run_config(Yp3Run(Path("x.elf"), 0, usb=True)) == {"return": "0", "usb": "1"}
    try:
        Yp3.run_config(Yp3Run(Path("x.elf"), 40, settings={"return": "9"}))
    except image.ImageError:
        pass
    else:
        raise AssertionError("--set return= passed")


# The flash writer, against a simulated flash

class FlashSim(rom.Rom):
    """The payload on a simulated player: flash, PMU registers and retained
    SRAM; records the sectors written and the resets"""

    def __init__(self, contents: bytes, retained_sram: bytes = b"") -> None:
        super().__init__(Path("/nonexistent/smtlink"), lambda: Path("/nonexistent/svc.bin"))
        self.nor = bytearray(contents)
        self.stage = b""
        self.written: list[int] = []
        self.resets: list[rom.Op] = []
        self.pmu = {power.PMU_CHARGE: 0x0C, power.PMU_ADC: 0x01, power.PMU_BATTERY: 0x50}
        self.retained = retained_sram

    def call(self, op: rom.Op, a0: int = 0, a1: int = 0, a2: int = 0, *,
             stage: bytes | None = None, usb_timeout_ms: int | None = None) -> rom.Reply:
        st = rom.Status.OK
        value = 0
        if op in (rom.Op.BOOT, rom.Op.RESET_TO_ROM):
            self.resets.append(op)
            raise rom.DeviceError("smtlink rc=1: the device went away")
        if op == rom.Op.PMU_READ:
            value = self.pmu.get(a0, 0)
        elif op == rom.Op.PMU_WRITE:
            self.pmu[a0] = a1
        elif op == rom.Op.FLASH_CRC:
            self.stage = b"".join(struct.pack("<I", rom.sector_crc(
                bytes(self.nor[a0 + i * rom.SECTOR:a0 + (i + 1) * rom.SECTOR])))
                for i in range(a1))
        elif op == rom.Op.FLASH_SECTOR:
            assert stage is not None and rom.sector_crc(stage) == a1
            assert rom.WRITABLE[0] <= a0 < rom.WRITABLE[1]
            self.nor[a0:a0 + rom.SECTOR] = stage
            self.written.append(a0)
        return rom.Reply(st, (rom.REPLY_MAGIC, op, st, value, 0, 0, *[0] * 10))

    def read_ram(self, addr: int, n: int) -> bytes:
        if addr == retained.RETAINED[0]:
            return self.retained[:n]
        assert addr == rom.STAGE
        return self.stage[:n]


def test_flash_writes_only_the_writable_changes() -> None:
    old = bytes(rom.IMAGE_SIZE)
    new = bytearray(b"\xaa" * rom.IMAGE_SIZE)
    new[0x20000:0x22000] = old[0x20000:0x22000]       # two sectors unchanged
    sim = FlashSim(old)
    out: list[str] = []
    sim.flash(bytes(new), out.append)
    lo, hi = rom.WRITABLE
    assert sim.nor[:lo] == old[:lo] and sim.nor[hi:] == old[hi:]
    assert sim.nor[lo:hi] == new[lo:hi]
    assert 0x20000 not in sim.written and 0x21000 not in sim.written
    assert len(sim.written) == (hi - lo) // rom.SECTOR - 2
    assert out[0].startswith("warning: image differs in protected sectors")
    for addr in (0, lo - rom.SECTOR, hi, lo + 1):
        try:
            sim.write_sector(addr, bytes(rom.SECTOR))
        except ValueError:
            continue
        raise AssertionError(f"sector {addr:#x} accepted")


def test_run_flow() -> None:
    """auto_rockbox.run.run through Yp3 on the simulated player: charger guard,
    image with the run config and a push, boot, return, log, charger off"""
    if not have_port():
        raise Skip(NO_PORT)
    import auto_rockbox.targets.yp3 as pkg  # noqa: PLC0415

    with tempfile.TemporaryDirectory() as tmp:
        t0 = Path(tmp)
        base = t0 / "base.bin"
        base.write_bytes(random.Random(5).randbytes(rom.IMAGE_SIZE))
        push = t0 / "cfg"
        push.write_bytes(b"volume: -20\n")
        ws = Workspace(t0, {"targets": {"yp3": {"base_image": str(base)}}})
        t = Yp3(ws)
        raw = _log_area([0] * 30, b"", text=b"--- boot 1 ---\nkeys: done\n", head=26)
        sim = FlashSim(base.read_bytes(), bytes(raw))
        t.__dict__["_transport"] = sim
        here, back = rom.UsbId("1-2", 1, 7), rom.UsbId("1-2", 1, 8)

        def returned(deadline: float, previous: rom.UsbId | None = None,
                     tick: object = None) -> rom.UsbId | None:
            return back if previous == here else None

        with (patch.object(pkg, "rom_devices", lambda: [here]),
              patch.object(rom, "rom_devices", lambda: [here]),
              patch.object(pkg, "wait_rom", returned)):
            r = run(t, Yp3Run(ELF, 40, "S 1500 D", {"play": "/a.mp3"},
                              pushes=[(push, "/.rockbox/t.cfg")]))
        assert r.returned and r.log.startswith("--- boot 1 ---\nkeys: done\n"), r.log
        assert sim.resets == [rom.Op.BOOT]
        cfg = bytes(sim.nor[image.DEV_CONFIG:image.DEV_CONFIG + 64])
        assert cfg.startswith(b"return=40\nkeys=S 1500 D\nplay=/a.mp3\n\xff"), cfg
        bundle = bytes(sim.nor[image.DEV_BUNDLE:image.DEV_BUNDLE + 64])
        assert bundle.startswith(image.sdpush_bundle([("/.rockbox/t.cfg", push.read_bytes())]))
        assert not sim.pmu[power.PMU_CHARGE] & power.CHARGE_ON      # off after the return
        assert sim.nor[:0xE000] == base.read_bytes()[:0xE000]
        lines = (t0 / "build/battery.log").read_text().splitlines()
        assert len(lines) == 2 and lines[1].endswith("-> charging disabled"), lines


# Retained SRAM

def _log_area(trace_pcs: list[int], snap: bytes, cop: bytes = b"",
              text: bytes = b"", head: int | None = None) -> bytearray:
    raw = bytearray(random.Random(3).randbytes(retained.RETAINED[1]))
    off = 0x1000
    blocks = struct.pack("<2I", retained.TRACE_MAGIC, 33) + struct.pack("<30I", *trace_pcs)
    blocks += cop + snap
    data_at = off + retained.LOG_HEADER + len(blocks)
    n = len(raw) - data_at
    ring = (text * (n // len(text) + 1))[:n] if text else bytes(n)
    raw[off:data_at] = struct.pack("<4I", retained.LOG_MAGIC,
                                   n * 2 + 100 if head is None else head, 3, 0) + blocks
    raw[data_at:] = ring
    for o in range(0, off, 4):          # nothing before the header looks like one
        if raw[o:o + 4] == struct.pack("<I", retained.LOG_MAGIC):
            raw[o] ^= 0xFF
    return raw


def _snapshot(elf: Elf, funcs: list[int]) -> bytes:
    base, size = elf.symbols["__thread_entries"]
    nslots, ntasks, nret = 4, 2, 3
    entry = size // nslots
    name = next(s.vaddr + s.data.index(b"yp3 keys\0") for s in elf.segments
                if b"yp3 keys\0" in s.data)
    body = struct.pack("<4B3I", nslots, ntasks, nret, 0, 1234, 0, base + 2 * entry)
    body += struct.pack("<2I", funcs[0] | 1, 0)
    slots = [(0x0100, name, 0x80A000, 0x80ABCD, [funcs[1] + 2, funcs[2] + 2, 0], 2, 10, 10),
             (0, 0, 0, 0, [0, 0, 0], 0, 0, 0),
             (0x0202, name, 0x80B000, 0, [funcs[3] + 2, 0, 0], 1, 16, 16),
             (0x0005, 0, 0x80C000, 0, [0, 0, 0], 3, 2, 2)]
    for tid, nm, sp, wqp, rets, st, pr, bp in slots:
        body += struct.pack("<4I3I4B", tid, nm, sp, wqp, *rets, st, pr, bp, 0)
    return struct.pack("<2I", retained.SNAP_MAGIC, 8 + len(body)) + body


def _fault(pc: int, lr: int, good_crc: bool = True) -> bytes:
    w = list(range(100, 132))
    w[0], w[26], w[27] = retained.FAULT_MAGIC, lr, pc
    raw = struct.pack("<29I", *w[:29])
    crc = binascii.crc32(raw + struct.pack("<I", w[29])) & 0xFFFFFFFF
    return raw + struct.pack("<3I", w[29], crc ^ (0 if good_crc else 1), retained.FAULT_COMMIT)


def test_retained_against_yp3_py() -> None:
    if not have_port():
        raise Skip(NO_PORT)
    yp3 = _yp3dev()
    elf = Elf(ELF)
    funcs = functions_at(40)
    pcs = [(f & 0xFFFFFF) | ((i % 3) << 24) for i, f in enumerate(funcs[:30])]
    pcs[5] = pcs[4]                         # a repeat to fold
    cop = struct.pack("<2I", retained.COP_MAGIC, 5) + struct.pack("<16I", *funcs[30:40], *[0] * 6)
    cop += struct.pack("<4I", 3, funcs[0], funcs[1] | 1, 0x80F000)
    text = b"--- boot 3 ---\nhello\nworld\n"
    for raw in (_log_area(pcs, _snapshot(elf, funcs), text=text),
                _log_area(pcs, _snapshot(elf, funcs), cop, text, head=50),
                _log_area(pcs, b"", text=text)):
        raw[0xF80:0x1000] = _fault(funcs[7], funcs[8] | 1)
        assert retained.decode_log(bytes(raw), names()) == yp3.decode_log(bytes(raw), ELF)
        assert retained.decode_threads(bytes(raw), elf, names()) == \
            yp3.decode_threads(bytes(raw), ELF)
        off = retained.find_magic(bytes(raw), retained.FAULT_MAGIC)
        assert off == 0xF80
        ours = retained.decode_fault(bytes(raw), Symbolizer(ELF, f"{TOOLCHAIN}addr2line").names)
        theirs = yp3.decode_fault(bytes(raw[off:off + 128]), ELF)
        assert ours.splitlines()[:2] == theirs.splitlines()[:2], (ours, theirs)
        assert ours.splitlines()[2].split()[1] == theirs.splitlines()[2].split()[1]
    raw[0xF80:0x1000] = _fault(funcs[7], funcs[8], good_crc=False)
    assert "(CRC mismatch)" in retained.decode_fault(bytes(raw))
    blank = bytes(retained.RETAINED[1])
    assert retained.decode_log(blank, names()) == yp3.decode_log(blank, ELF)
    assert retained.decode_fault(blank) == "no fault record"
    assert retained.decode_threads(blank, elf, names()) == yp3.decode_threads(blank, ELF)
    assert retained.trace_names(pcs, names()) == yp3.symbolize(pcs, ELF)
    line = "  ticks: " + " ".join(f"{p:08x}" for p in pcs[:6])
    assert retained.annotate_ticks(line, names()) == line + "\n    = " + yp3.symbolize(
        [int(w, 16) for w in line.split()[1:]], ELF)
    assert retained.annotate_ticks("ok", names()) == "ok"


def test_retained_alone() -> None:
    """The ring unwrapped at a wrap, without an ELF"""
    def unknown(addrs: list[int]) -> list[str]:
        return ["??"] * len(addrs)

    text = b"0123456789"
    raw = _log_area([0x01C12345] * 30, b"", text=text, head=10)
    assert retained.decode_log(bytes(raw), unknown).startswith("0123456789\nlast tick PCs")
    raw = _log_area([0] * 30, b"", text=text)
    n = len(raw) - (0x1000 + retained.LOG_HEADER + 128)
    head = n * 2 + 100
    log = retained.decode_log(bytes(raw), unknown)
    ring = (text * (n // len(text) + 1))[:n]
    assert log.split("\n" + "last tick")[0] == (ring[head % n:] + ring[:head % n]).decode()
    assert retained.trace_names([0x01C12345, 0x01C12345, 0x00C00010], unknown) == \
        "c12345@0 x2, c00010"


# Screenshots

def test_shot_refetches_lost_rows() -> None:
    lcd = Yp3.lcd
    rng = random.Random(4)
    rows = {y: rng.randbytes(2 * lcd.width) for y in range(lcd.height)}
    asked: list[str] = []

    def send(line: str) -> Reply:
        asked.append(line)
        parts = line.split()
        ys = range(lcd.height) if len(parts) == 1 else range(int(parts[1]), int(parts[2]) + 1)
        lost = {7, 8, 9, 159} if len(parts) == 1 else set()
        lines = [f"fb {y} {rows[y].hex()}"[:50 if y == 20 and len(parts) == 1 else None]
                 for y in ys if y not in lost]        # row 20 cut mid-line
        return Reply(True, [*lines, "ok"])

    im, missing = shot.screenshot(send, lcd)
    assert asked == ["shot", "shot 7 9", "shot 20 20", "shot 159 159"] and not missing
    for y in (0, 8, 20, 159):
        for x in (0, 77, 127):
            v = rows[y][2 * x] << 8 | rows[y][2 * x + 1]       # yp3.py cmd_shot's pixel
            assert im.getpixel((x, y)) == ((v >> 11) * 255 // 31, (v >> 5 & 63) * 255 // 63,
                                           (v & 31) * 255 // 31)
    assert shot.missing_runs([1, 2, 3, 7, 9, 10]) == [(1, 3), (7, 7), (9, 10)]


# Profiles

def test_profile_matches_profile_py() -> None:
    if not have_port():
        raise Skip(NO_PORT)
    funcs = Elf(ELF).functions
    pcs = functions_at(40)
    dump = "\n".join(" ".join(f"{p + 2 * (i % 3):x}" for i, p in enumerate(pcs[k:k + 10]))
                     for k in range(0, 40, 10)) + "\nprof: 40 samples\nok\n"
    dump = dump + dump.split("\n", 1)[0] + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "prof.txt"
        f.write_text(dump.replace("prof: 40 samples\n", ""))
        want = subprocess.run([sys.executable, str(YP3DEV / "profile.py"), str(f), str(ELF)],
                              check=True, capture_output=True, text=True).stdout
    got = prof.report(prof.samples(dump), funcs)
    assert got == want.rstrip("\n"), (got, want)


# Payload and command line

def test_payload_builds() -> None:
    if not all(shutil.which(t) for t in ("clang", "ld.lld", "llvm-objcopy")):
        raise Skip("no clang, ld.lld and llvm-objcopy on PATH")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        binary = rom.build_payload(out)
        stamp = (out / "svc.stamp").stat().st_mtime_ns
        assert rom.build_payload(out) == binary and (out / "svc.stamp").stat().st_mtime_ns == stamp
        assert 0 < binary.stat().st_size <= 0xA00
        old = YP3DEV / "build/svc.bin"
        if old.exists():
            assert binary.read_bytes() == old.read_bytes()


def test_commands() -> None:
    ws = Workspace(Path(tempfile.gettempdir()))
    registry.load_plugins(ws)
    have = set(registry.commands_for(Yp3))
    want = {"run", "flash", "package", "boot", "rom", "wait", "state", "read-flash", "read-ram",
            "r32", "w32", "pmu", "log", "fault", "threads", "sym", "shot", "stock",
            "charge-log", "profile", "battery", "sh", "keys"}
    assert want <= have, want - have
    assert registry.commands_for(Yp3)["run"].targets == frozenset({"yp3"})
    t = Yp3(ws)
    t.check_keys("S 1500 D S UL+ SU* D~")
    assert t.default_elf == ws.build_dir / "yp3/rockbox.elf"
    assert power.millivolts(0x50) == int(2800 + 14.173 * 0x50)


if __name__ == "__main__":
    from tests.__main__ import main

    sys.exit(main(["yp3"]))
