"""The TS17 flash controller's read randomizer: a model we fitted to dumps of
the player's NOR (the stored cells against what the CPU reads).

With the randomizer on (NOR_CTL bits 0x20001000) the controller descrambles
what it reads; writes store bytes as given. So an image is encoded here
before it is written. XIP reads restart the keystream every 32-byte line, the
boot ROM reads its first stage in 512-byte runs. One loop does both
directions: the feedback always comes from the stored (cell) byte.
"""

from __future__ import annotations

import numpy as np
import numpy.typing as npt

XIP_RUN = 32                    # XIP cache line: what Rockbox runs from
BOOT_RUN = 512                  # the boot ROM's runs, for a boot-stage image

# Keystream and feedback-pattern seeds, each extended by one tap set to a run
_K0 = bytes.fromhex("ceb9b0b3866bd0759d1eda96bc5cebb51f5a63137006dc28c1c698246ca8c3c282a1946c")
_G0 = bytes.fromhex("0002878b1060060d1e7fbc7cbffc3ef9152a93a06345aa7142c62cbfff1f4d8892475ac2")
_TAPS = (1, 4, 5, 6, 8, 10, 12, 13, 22, 26, 27, 28, 30, 31)


def _extend(seed: bytes, n: int = BOOT_RUN) -> npt.NDArray[np.uint8]:
    x = bytearray(seed)
    for i in range(len(seed), n):
        v = 0
        for e in _TAPS:
            v ^= x[i - e]
        x.append(v)
    return np.frombuffer(bytes(x), dtype=np.uint8)


KEY = _extend(_K0)
FEEDBACK = _extend(_G0)
# The pattern applies from the next byte on when bits 5:2 of the cell byte
# have odd parity
_ODD = np.array([(c & 0x3C).bit_count() & 1 for c in range(256)], dtype=bool)


def _transform(data: bytes, run: int, encode: bool) -> bytes:
    if len(data) % run:
        raise ValueError(f"length {len(data):#x} is not a multiple of the run ({run})")
    src = np.frombuffer(data, dtype=np.uint8).reshape(-1, run)
    out = np.empty_like(src)
    mask = np.tile(KEY[:run], (src.shape[0], 1))
    for b in range(run):
        out[:, b] = src[:, b] ^ mask[:, b]
        hit = _ODD[out[:, b] if encode else src[:, b]]
        if b + 1 < run and hit.any():
            mask[hit, b + 1:] ^= FEEDBACK[1:run - b]
    return out.tobytes()


def encode(plain: bytes, run: int = XIP_RUN) -> bytes:
    """Plain bytes -> the cells to program, so that a randomized read returns
    them plain"""
    return _transform(plain, run, True)


def decode(cells: bytes, run: int = XIP_RUN) -> bytes:
    """Stored cells -> what a randomized read returns"""
    return _transform(cells, run, False)
