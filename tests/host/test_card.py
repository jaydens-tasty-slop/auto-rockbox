"""The card check on FAT images (fsck.fat, as with the uaccess rule): a clean
one, a damaged one's report, and a repair."""

from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path

from auto_rockbox import card


def fat_image(path: Path, damaged: bool) -> None:
    """A 1 MiB FAT12 image; DAMAGED: its second FAT differs from the first"""
    path.unlink(missing_ok=True)        # mkfs.fat -C makes a new file only
    subprocess.run(["mkfs.fat", "-C", str(path), "1024"], check=True, capture_output=True)
    if damaged:
        b = bytearray(path.read_bytes())
        bps, _, reserved = struct.unpack_from("<HBH", b, 11)
        fat_sectors = struct.unpack_from("<H", b, 22)[0]
        b[(reserved + fat_sectors) * bps + 10] = 0xFF
        path.write_bytes(b)


@unittest.skipUnless(shutil.which("mkfs.fat") and shutil.which("fsck.fat"), "dosfstools")
class TestCheck(unittest.TestCase):
    def test_clean_damaged_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            img = Path(d) / "card.img"
            fat_image(img, damaged=False)
            assert card.check(img).describe() == "fsck: clean"
            fat_image(img, damaged=True)
            c = card.check(img)
            assert not c.clean and "FATs differ" in c.report
            assert "FATs differ" in c.describe()
            c = card.check(img, repair=True)
            assert c.clean and "FATs differ" in c.repaired
            assert card.check(img).describe() == "fsck: clean"


if __name__ == "__main__":
    unittest.main()
