"""WAV and FLAC in and out, and Line In captures as one recording."""

from __future__ import annotations

import io
import shutil
import subprocess
import wave
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

import numpy as np

from ..errors import ArbError
from ..linein import Second


class AudioError(ArbError):
    pass


def write_wav(path: Path, x: np.ndarray, fs: int, bits: int = 16, *, dither: bool = True,
              seed: int = 1) -> None:
    """Float frames (n, ch) as PCM. TPDF dither on non-zero samples only, so
    the signal's digital silence stays digital silence."""
    q = 2 ** (bits - 1)
    y = x * (q - 1)
    if dither:
        r = np.random.default_rng(seed)
        y = np.where(x != 0, y + r.random(y.shape) - r.random(y.shape), 0)
    y = np.clip(np.round(y), -q, q - 1).astype("<i4")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(x.shape[1])
        w.setsampwidth(bits // 8)
        w.setframerate(fs)
        if bits == 16:
            w.writeframes(y.astype("<i2").tobytes())
        else:
            w.writeframes(y.view("u1").reshape(-1, 4)[:, :3].tobytes())


def read_wav(f: str | BinaryIO) -> tuple[np.ndarray, int]:
    """Frames (n, ch) in full-scale units, and the sample rate"""
    with wave.open(f, "rb") as w:
        fs, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(raw, "<i2").astype(float) / 32768
    elif sw == 3:
        b = np.frombuffer(raw, "u1").reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        a = np.where(v >= 1 << 23, v - (1 << 24), v).astype(float) / 2 ** 23
    else:
        a = np.frombuffer(raw, "<i4").astype(float) / 2 ** 31
    return a.reshape(-1, ch), fs


def require_flac() -> str:
    exe = shutil.which("flac")
    if not exe:
        raise AudioError("FLAC needs the flac command line tool (the flac package)")
    return exe


def read_audio(path: Path) -> tuple[np.ndarray, int]:
    """A WAV or FLAC file"""
    if path.suffix.lower() == ".flac":
        data = subprocess.run([require_flac(), "-dcs", str(path)], check=True,
                              capture_output=True).stdout
        return read_wav(io.BytesIO(data))
    return read_wav(str(path))


def flac_encode(wav: Path, flac: Path, *, keep_wav: bool = True) -> None:
    cmd = [require_flac(), "-s", "-f", "-8", "-o", str(flac), str(wav)]
    if not keep_wav:
        cmd.insert(3, "--delete-input-file")
    subprocess.run(cmd, check=True)


def load_capture(seconds: Sequence[Second]) -> tuple[np.ndarray, int]:
    """Line In seconds, in the order given, as one recording"""
    if not seconds:
        raise AudioError("no capture seconds in that window")
    parts = [s.samples() for s in seconds]
    return np.concatenate([x for x, _ in parts]), parts[0][1]
