"""Test media for codec and video runs, made from one tone file.

The tone (1 kHz left, 1.5 kHz right, stepping down 12 dB partway) is encoded
into a short clip per codec Rockbox plays and into test videos, so Line In
can tell that each file plays whole. A clip that ends after the step-down
ends near Line In's noise floor: that is not a gap between tracks. Copied
files (high-rate music, a tracker module, real videos) complete the set.

OUT/codecs/NN_name.ext and OUT/video/NN_name.ext (NN fixes the file
browser's order) and OUT/manifest.json, which lists every file with its kind
("stereo": 1 kHz left and 1.5 kHz right; "tone": the tones on other
channels, as mono and 1 kHz-only files; "music"; "unknown": undecodable
here), its duration and when its 12 dB step-down comes (null for none).
Formats ffmpeg cannot write (APE, Musepack, Shorten, RealAudio, ATRAC3, WMA
Pro, the game-music formats but MOD) are left out.

    [media]
    tone = "build/audio/tone.wav"
    extra = "build/audio/codecs"            # copied clips (optional)
    mod = "path/to/module.mod"              # a tracker module (optional)
    videos = ["path/to/video.avi"]          # copied videos (optional)
    video_size = "160x128"                  # the test videos' picture
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .linein import dominant_hz, tone_kind

CLIP_S = 12
RATE = 48000                # decoding rate for the analysis
VOX_RATE = 8000
STEP_DB = 12.0

# (name, ffmpeg output arguments); the extension picks Rockbox's codec
CODECS: list[tuple[str, list[str]]] = [
    ("mp3_192.mp3", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    ("mp2_192.mp2", ["-c:a", "mp2", "-b:a", "192k"]),
    ("aac_128.m4a", ["-c:a", "aac", "-b:a", "128k"]),
    ("aac_adts.aac", ["-c:a", "aac", "-b:a", "128k", "-f", "adts"]),
    ("alac.m4a", ["-c:a", "alac"]),
    ("flac.flac", ["-c:a", "flac"]),
    ("wav_pcm.wav", ["-c:a", "pcm_s16le"]),
    ("wav_ima.wav", ["-c:a", "adpcm_ima_wav"]),
    ("wave64.w64", ["-c:a", "pcm_s16le", "-f", "w64"]),
    ("aiff.aiff", ["-c:a", "pcm_s16be"]),
    ("au.au", ["-c:a", "pcm_s16be", "-f", "au"]),
    ("tta.tta", ["-c:a", "tta"]),
    ("wavpack.wv", ["-c:a", "wavpack"]),
    ("vorbis_q5.ogg", ["-c:a", "libvorbis", "-q:a", "5"]),
    ("opus_128.opus", ["-c:a", "libopus", "-b:a", "128k"]),
    ("speex_wb.spx", ["-ac", "1", "-ar", "16000", "-c:a", "libspeex"]),
    ("ac3_192.ac3", ["-c:a", "ac3", "-b:a", "192k"]),
    ("wma_128.wma", ["-c:a", "wmav2", "-b:a", "128k"]),
    ("adx.adx", ["-c:a", "adpcm_adx", "-f", "adx"]),
    ("smaf.mmf", ["-ac", "1", "-ar", "22050", "-c:a", "adpcm_yamaha", "-f", "mmf"]),
]

# (name, ffmpeg output arguments) for a test picture with the tone
VIDEOS: list[tuple[str, list[str]]] = [
    ("mpeg1.mpg", ["-c:v", "mpeg1video", "-b:v", "400k", "-c:a", "mp2", "-b:a", "128k",
                   "-f", "mpeg"]),
    ("mpeg2.mpg", ["-c:v", "mpeg2video", "-b:v", "400k", "-c:a", "mp2", "-b:a", "128k",
                   "-f", "vob"]),
    ("mpeg1.mkv", ["-c:v", "mpeg1video", "-b:v", "400k", "-bf", "0", "-g", "25",
                   "-c:a", "mp2", "-b:a", "128k"]),
    ("mjpeg.avi", ["-c:v", "mjpeg", "-q:v", "5", "-c:a", "pcm_s16le"]),
]

# Low priority and few threads: the bench's capture publishers and test
# runs beside this are timing-sensitive
FFMPEG = ["nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-threads", "4"]


def _encode(inputs: list[str], args: list[str], out: Path) -> None:
    subprocess.run([*FFMPEG, "-y", *inputs, *args, "-threads", "4", str(out)], check=True)


def decode(path: Path, *, start: float = 0.0, seconds: float | None = None,
           rate: int = RATE, channels: int = 2) -> np.ndarray:
    """The audio as int16 frames (n, CHANNELS); empty if ffmpeg cannot read it"""
    cmd = [*FFMPEG, "-ss", str(start), *(["-t", str(seconds)] if seconds else []),
           "-i", str(path), "-vn", "-ac", str(channels), "-ar", str(rate), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=False).stdout
    frame = 2 * channels
    return np.frombuffer(raw[:len(raw) // frame * frame], "<i2").reshape(-1, channels)


def duration(path: Path) -> float | None:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=False)
    try:
        return round(float(r.stdout.strip()), 2)
    except ValueError:
        return None


def classify(path: Path) -> str:
    """The kind of a file, from the spectrum of seconds 1-4 (past any fade-in).
    ffmpeg cannot read raw VOX (no header): by its name, a mono tone."""
    a = decode(path, start=1.0, seconds=3.0).astype(np.float64)
    if len(a) < RATE:
        return "tone" if "vox" in path.name.lower() else "unknown"
    return tone_kind(dominant_hz(a[:, 0], RATE), dominant_hz(a[:, 1], RATE))


def step_down(a: np.ndarray, rate: int = RATE) -> float | None:
    """Where the level falls by about STEP_DB and stays down for half a
    second, in s; a fall to silence (the end) is no step"""
    n = rate // 20                                          # 50 ms windows
    m = a.astype(np.float64).mean(axis=1)
    blocks = m[:len(m) // n * n].reshape(-1, n)
    if not len(blocks):
        return None
    lv = 10 * np.log10((blocks ** 2).mean(axis=1) + 1e-20)
    onset = int(np.argmax(lv > lv.max() - 30))
    ref = float(np.median(lv[onset:onset + 20]))            # its first second
    down = ((lv < ref - STEP_DB + 3) & (lv > ref - STEP_DB - 3)).astype(int)
    hold = 10
    held = np.flatnonzero(np.convolve(down, np.ones(hold, int), "valid") == hold)
    held = held[held >= onset + 20]
    return round(float(held[0]) * n / rate, 2) if len(held) else None


# Dialogic (OKI) ADPCM: 12-bit samples, 4-bit codes, high nibble first
OKI_STEPS = [16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80,
             88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337,
             371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963, 1060, 1166, 1282,
             1411, 1552]
OKI_ADJUST = [-1, -1, -1, -1, 2, 4, 6, 8]


def oki_adpcm(samples: np.ndarray) -> bytes:
    """VOX data for 16-bit SAMPLES; ffmpeg decodes OKI ADPCM but cannot write
    it. Sequential by nature: each code depends on the decoder state."""
    pred, idx, codes = 0, 0, []
    for s in samples.tolist():
        step = OKI_STEPS[idx]
        diff = (s >> 4) - pred
        code = 8 if diff < 0 else 0
        diff = abs(diff)
        delta = step >> 3
        for bit, part in ((4, step), (2, step >> 1), (1, step >> 2)):
            if diff >= part:
                code |= bit
                diff -= part
                delta += part
        pred = max(-2048, min(2047, pred - delta if code & 8 else pred + delta))
        idx = max(0, min(48, idx + OKI_ADJUST[code & 7]))
        codes.append(code)
    if len(codes) % 2:
        codes.append(0)
    return bytes(a << 4 | b for a, b in zip(codes[0::2], codes[1::2], strict=True))


@dataclass(frozen=True)
class Entry:
    path: str                   # relative to the output directory
    kind: str
    duration: float | None      # s
    step_down: float | None     # s from the start to the 12 dB step


def describe(root: Path, path: Path, kind: str | None = None) -> Entry:
    kind = kind or classify(path)
    step = step_down(decode(path)) if kind in ("stereo", "tone") else None
    return Entry(str(path.relative_to(root)), kind, duration(path), step)


def make(out: Path, tone: Path, *, extra: Path | None = None, mod: Path | None = None,
         videos: Sequence[Path] = (), video_size: str = "160x128",
         echo: Callable[[str], object] = print) -> list[Entry]:
    """Writes the media and OUT/manifest.json; returns the manifest's entries"""
    if not tone.is_file():
        raise FileNotFoundError(f"no tone file {tone}")
    tone_step = step_down(decode(tone))
    clip = ["-t", str(CLIP_S)]

    def codec(name: str, args: list[str]) -> Callable[[Path], Entry]:
        def run(p: Path) -> Entry:
            _encode(["-i", str(tone)], [*clip, "-map_metadata", "-1",
                                        "-metadata", f"title={name}", *args], p)
            return describe(out, p)
        return run

    def vox(p: Path) -> Entry:
        pcm = decode(tone, seconds=CLIP_S, rate=VOX_RATE, channels=1)
        p.write_bytes(oki_adpcm(pcm[:, 0]))
        step = tone_step if tone_step is not None and tone_step < CLIP_S else None
        return Entry(str(p.relative_to(out)), "tone", round(len(pcm) / VOX_RATE, 2), step)

    def video(args: list[str]) -> Callable[[Path], Entry]:
        def run(p: Path) -> Entry:
            pix = "yuvj420p" if "mjpeg" in p.name else "yuv420p"
            _encode(["-f", "lavfi", "-i", f"testsrc2=size={video_size}:rate=25",
                     "-i", str(tone)], [*clip, "-pix_fmt", pix, *args], p)
            return describe(out, p)
        return run

    def copy(src: Path, kind: str | None = None) -> Callable[[Path], Entry]:
        def run(p: Path) -> Entry:
            shutil.copyfile(src, p)
            return describe(out, p, kind)
        return run

    extras = sorted(f for f in extra.iterdir() if f.is_file()) if extra else []
    folders: dict[Path, list[tuple[str, Callable[[Path], Entry]]]] = {
        out / "codecs": [*((name, codec(name, args)) for name, args in CODECS),
                         ("vox_8k.vox", vox),
                         # a tracker module is music by what it is; ffmpeg may lack its decoder
                         *([(mod.name, copy(mod, "music"))] if mod else []),
                         *((f.name, copy(f)) for f in extras)],
        out / "video": [*((name, video(args)) for name, args in VIDEOS),
                        *((f.name, copy(f)) for f in videos)],
    }
    entries: list[Entry] = []
    for folder, jobs in folders.items():
        folder.mkdir(parents=True, exist_ok=True)
        for n, (name, job) in enumerate(jobs, 1):
            e = job(folder / f"{n:02d}_{name}")
            entries.append(e)
            echo(f"{e.path:40} {e.kind:8} {e.duration or 0:7.2f} s  step {e.step_down}")

    manifest = {"tone": str(tone), "clip_seconds": CLIP_S, "tone_step_down": tone_step,
                "files": [asdict(e) for e in entries]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return entries
