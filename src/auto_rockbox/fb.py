"""Framebuffer bytes to a picture."""

from __future__ import annotations

import numpy as np
from PIL import Image

from .target import Lcd


def decode(raw: bytes, lcd: Lcd, big_endian: bool | None = None) -> Image.Image:
    """RGB565 pixels, row by row: as the LCD keeps them in memory (LCD.format
    "rgb565-swapped": each pixel's two bytes the other way round, as some LCD
    FIFOs want them), or each value's high byte first if BIG_ENDIAN"""
    n = lcd.width * lcd.height
    if len(raw) < 2 * n:
        raise ValueError(f"framebuffer: {len(raw)} bytes, {2 * n} needed")
    if big_endian is None:
        big_endian = lcd.format == "rgb565-swapped"
    order = ">u2" if big_endian else "<u2"
    px = np.frombuffer(raw[:2 * n], dtype=order).astype(np.uint32)
    rgb = np.stack([(px >> 11) * 255 // 31, ((px >> 5) & 63) * 255 // 63, (px & 31) * 255 // 31],
                   axis=-1).astype(np.uint8)
    return Image.fromarray(rgb.reshape(lcd.height, lcd.width, 3), "RGB")


def _crc_table() -> list[int]:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else c << 1
        table.append(c & 0xFFFFFFFF)
    return table


_CRC_TABLE = _crc_table()


def crc32_mpeg2(data: bytes) -> int:
    """Rockbox's crc_32(data, len, 0xffffffff) (CRC-32/MPEG-2): what a dev
    build logs of its framebuffer, to check a read against"""
    crc = 0xFFFFFFFF
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((crc >> 24) ^ b) & 0xFF]
    return crc


def scaled(im: Image.Image, factor: int = 3) -> Image.Image:
    """Bigger, with hard pixel edges: easier to read than the panel's own size"""
    return im.resize((im.width * factor, im.height * factor), Image.Resampling.NEAREST)
