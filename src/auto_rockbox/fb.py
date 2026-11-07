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


def scaled(im: Image.Image, factor: int = 3) -> Image.Image:
    """Bigger, with hard pixel edges: easier to read than the panel's own size"""
    return im.resize((im.width * factor, im.height * factor), Image.Resampling.NEAREST)
