"""The randomizer model against the ts17dev tool it came from."""

from __future__ import annotations

import hashlib
import importlib.util
import random
from pathlib import Path
from types import ModuleType

from auto_rockbox.targets.ts17 import rdm

from . import Skip

OLD = Path(__file__).resolve().parents[3] / "ts17dev" / "rdm.py"
# sha256 of encode/decode of random.Random(2157).randbytes(0x2000), from ts17dev/rdm.py
KNOWN = {
    32: ("8eb16fcb32b5ac27b1716564c3d6509027425b7f9ca1437e7855ab61ce0a62ed",
         "bd3c5c83d5234ababc9803c31f10bf8f04ab638fd02e4f5935c820267ceaa3f9"),
    512: ("c9d2c27801c58125936b63e8f6683f348d8474d6b97d10064bbbb2ebf7f838b0",
          "dfd4da9b7064c94143fb04fa56cd81c1fa0f2ba2c9b70eca135066c271244df9"),
}


def _old() -> ModuleType:
    if not OLD.exists():
        raise Skip(f"{OLD} is gone")
    spec = importlib.util.spec_from_file_location("ts17dev_rdm", OLD)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_tables() -> None:
    assert rdm.KEY[504:512].tobytes().hex() == "9c8ffe4fbaf307c8"
    assert rdm.FEEDBACK[504:512].tobytes().hex() == "95b9c2847e8b2046"


def test_known_answers() -> None:
    data = random.Random(2157).randbytes(0x2000)
    for run, (enc, dec) in KNOWN.items():
        assert hashlib.sha256(rdm.encode(data, run)).hexdigest() == enc, run
        assert hashlib.sha256(rdm.decode(data, run)).hexdigest() == dec, run


def test_round_trip() -> None:
    data = random.Random(1).randbytes(0x1000)
    for run in (rdm.XIP_RUN, rdm.BOOT_RUN):
        assert rdm.decode(rdm.encode(data, run), run) == data
        assert rdm.encode(data, run) != data


def test_matches_ts17dev() -> None:
    old = _old()
    rng = random.Random(17)
    for n in (32, 0x400, 0x1E00, 0x10000):
        data = rng.randbytes(n)
        assert rdm.encode(data) == old.encode(data), n
        assert rdm.decode(data) == old.decode(data), n
    data = rng.randbytes(0xE00)
    assert rdm.encode(data, rdm.BOOT_RUN) == old.encode(data, old.BOOT_RUN)


def test_rejects_partial_run() -> None:
    try:
        rdm.encode(bytes(33))
    except ValueError:
        return
    raise AssertionError("33 bytes encoded in 32-byte runs")
