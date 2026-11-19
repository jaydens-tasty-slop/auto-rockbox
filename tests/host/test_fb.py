"""Framebuffer helpers: the CRC a dev build logs of its framebuffer."""

from __future__ import annotations

import unittest

from auto_rockbox import fb


class TestCrc(unittest.TestCase):
    def test_check_value(self) -> None:
        # CRC-32/MPEG-2's check value, as Rockbox's crc_32(..., 0xffffffff) gives
        assert fb.crc32_mpeg2(b"123456789") == 0x0376E6E7
        assert fb.crc32_mpeg2(b"") == 0xFFFFFFFF


if __name__ == "__main__":
    unittest.main()
