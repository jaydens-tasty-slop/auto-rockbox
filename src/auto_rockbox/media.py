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

The torture's codec set (OUT/codectest/, suites/rockbox_torture.py) is the
tone again, a CODECTEST_S clip per codec at the rates, depths and bitrates
that load each decoder most, plus WMA, which master cannot play (a file the
torture expects to fail).

The stress set is music instead: a STRESS_S clip of one source per codec,
bitrate and rate case, tagged "<codec> <settings>" so Now Playing shows what
plays, in OUT/stress/, and OUT/Playlists/Stress test.m3u8 to hammer through
them by hand or in a torture run (copy OUT's contents to the card root).
Cases past the codecs' documented limits on the player are left out on
purpose; block sizes are set where ffmpeg would copy the source's (16-sample
FLAC frames from a FLAC source decode far too slowly); HE-AAC and APE need
encoders ffmpeg lacks.

    [media]
    tone = "build/audio/tone.wav"
    extra = "build/audio/codecs"            # copied clips (optional)
    mod = "path/to/module.mod"              # a tracker module (optional)
    videos = ["path/to/video.avi"]          # copied videos (optional)
    video_size = "160x128"                  # the test videos' picture
    stress_source = "build/audio/codecs/flac_16_44.flac"   # music for the stress set
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .errors import ArbError
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

STRESS_S = 30
STRESS_PLAYLIST = "Playlists/Stress test.m3u8"
OGG_PAGES = ["-page_duration", "100000"]     # oggenc-sized pages, not 1 s

CODECTEST_S = 20
CODECTEST_DIR = "codectest"     # on the card root (rockbox_torture.CODECTEST)

# (name, ffmpeg output arguments) of the torture's codec set; the name says
# the depth and rate (kHz) where the codec has them
CODECTEST: list[tuple[str, list[str]]] = [
    ("aac_320.m4a", ["-c:a", "aac", "-b:a", "320k"]),
    ("ac3_640.ac3", ["-ar", "48000", "-c:a", "ac3", "-b:a", "640k"]),
    ("aiff_16_44.aiff", ["-ar", "44100", "-c:a", "pcm_s16be"]),
    ("alac_16_44.m4a", ["-ar", "44100", "-sample_fmt", "s16p", "-c:a", "alac"]),
    ("au_16_44.au", ["-ar", "44100", "-c:a", "pcm_s16be", "-f", "au"]),
    ("flac_16_44.flac", ["-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
                         "-frame_size", "1024"]),
    ("flac_16_44_std.flac", ["-ar", "44100", "-sample_fmt", "s16", "-c:a", "flac",
                             "-frame_size", "4096"]),
    ("flac_24_96.flac", ["-ar", "96000", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                         "-c:a", "flac", "-frame_size", "1024"]),
    ("mp3_320.mp3", ["-ar", "44100", "-c:a", "libmp3lame", "-b:a", "320k"]),
    ("opus_128.opus", ["-c:a", "libopus", "-b:a", "128k", *OGG_PAGES]),
    ("opus_510.opus", ["-c:a", "libopus", "-b:a", "510k", *OGG_PAGES]),
    ("speex_q10.spx", ["-ar", "32000", "-c:a", "libspeex", "-cbr_quality", "10"]),
    ("tta_16_44.tta", ["-ar", "44100", "-sample_fmt", "s16", "-c:a", "tta"]),
    ("vorbis_q10.ogg", ["-ar", "44100", "-c:a", "libvorbis", "-q:a", "10", *OGG_PAGES]),
    ("vorbis_q5.ogg", ["-ar", "44100", "-c:a", "libvorbis", "-q:a", "5", *OGG_PAGES]),
    ("wav_24_96.wav", ["-ar", "96000", "-c:a", "pcm_s24le"]),
    ("wma_320.wma", ["-ar", "44100", "-c:a", "wmav2", "-b:a", "320k"]),
    ("wv_16_44.wv", ["-ar", "44100", "-sample_fmt", "s16p", "-c:a", "wavpack"]),
    ("wv_24_96.wv", ["-ar", "96000", "-sample_fmt", "s32p", "-bits_per_raw_sample", "24",
                     "-c:a", "wavpack"]),
]

# (name, title, ffmpeg output arguments) of the stress set
STRESS: list[tuple[str, str, list[str]]] = [
    ("mp3_cbr032_22k_mono.mp3", "MP3 CBR 32k 22kHz mono",
     ["-ar", "22050", "-ac", "1", "-c:a", "libmp3lame", "-b:a", "32k"]),
    ("mp3_cbr064.mp3", "MP3 CBR 64k", ["-c:a", "libmp3lame", "-b:a", "64k"]),
    ("mp3_cbr128.mp3", "MP3 CBR 128k", ["-c:a", "libmp3lame", "-b:a", "128k"]),
    ("mp3_cbr192.mp3", "MP3 CBR 192k", ["-c:a", "libmp3lame", "-b:a", "192k"]),
    ("mp3_cbr256.mp3", "MP3 CBR 256k", ["-c:a", "libmp3lame", "-b:a", "256k"]),
    ("mp3_cbr320.mp3", "MP3 CBR 320k", ["-c:a", "libmp3lame", "-b:a", "320k"]),
    ("mp3_vbr_v0.mp3", "MP3 VBR V0", ["-c:a", "libmp3lame", "-q:a", "0"]),
    ("mp3_vbr_v5.mp3", "MP3 VBR V5", ["-c:a", "libmp3lame", "-q:a", "5"]),
    ("mp3_cbr192_48k.mp3", "MP3 CBR 192k 48kHz",
     ["-ar", "48000", "-c:a", "libmp3lame", "-b:a", "192k"]),
    ("mp3_cbr128_32k.mp3", "MP3 CBR 128k 32kHz",
     ["-ar", "32000", "-c:a", "libmp3lame", "-b:a", "128k"]),
    ("mp2_192.mp2", "MP2 192k", ["-c:a", "mp2", "-b:a", "192k"]),
    ("aac_lc_096.m4a", "AAC-LC 96k", ["-c:a", "aac", "-b:a", "96k"]),
    ("aac_lc_128.m4a", "AAC-LC 128k", ["-c:a", "aac", "-b:a", "128k"]),
    ("aac_lc_256.m4a", "AAC-LC 256k", ["-c:a", "aac", "-b:a", "256k"]),
    ("aac_lc_128_48k.m4a", "AAC-LC 128k 48kHz", ["-ar", "48000", "-c:a", "aac", "-b:a", "128k"]),
    ("alac_16_44.m4a", "ALAC 16/44", ["-c:a", "alac"]),
    ("vorbis_q0.ogg", "Vorbis q0", ["-c:a", "libvorbis", "-q:a", "0", *OGG_PAGES]),
    ("vorbis_q3.ogg", "Vorbis q3", ["-c:a", "libvorbis", "-q:a", "3", *OGG_PAGES]),
    ("vorbis_q5.ogg", "Vorbis q5", ["-c:a", "libvorbis", "-q:a", "5", *OGG_PAGES]),
    ("vorbis_q5_48k.ogg", "Vorbis q5 48kHz",
     ["-ar", "48000", "-c:a", "libvorbis", "-q:a", "5", *OGG_PAGES]),
    ("opus_032.opus", "Opus 32k", ["-c:a", "libopus", "-b:a", "32k", *OGG_PAGES]),
    ("opus_064.opus", "Opus 64k", ["-c:a", "libopus", "-b:a", "64k", *OGG_PAGES]),
    ("opus_128.opus", "Opus 128k", ["-c:a", "libopus", "-b:a", "128k", *OGG_PAGES]),
    ("opus_192.opus", "Opus 192k", ["-c:a", "libopus", "-b:a", "192k", *OGG_PAGES]),
    ("flac_16_44_l0.flac", "FLAC 16/44 level 0",
     ["-c:a", "flac", "-frame_size", "4096", "-compression_level", "0"]),
    ("flac_16_44_l8.flac", "FLAC 16/44 level 8",
     ["-c:a", "flac", "-frame_size", "4096", "-compression_level", "8"]),
    ("flac_16_48.flac", "FLAC 16/48", ["-ar", "48000", "-c:a", "flac", "-frame_size", "4096"]),
    ("flac_24_96.flac", "FLAC 24/96",
     ["-ar", "96000", "-sample_fmt", "s32", "-c:a", "flac", "-frame_size", "4096"]),
    ("wav_16_44.wav", "WAV 16/44", ["-c:a", "pcm_s16le"]),
    ("wav_16_22_mono.wav", "WAV 16/22 mono", ["-ar", "22050", "-ac", "1", "-c:a", "pcm_s16le"]),
    ("wav_24_96.wav", "WAV 24/96", ["-ar", "96000", "-c:a", "pcm_s24le"]),
    ("wav_ima_adpcm.wav", "WAV IMA ADPCM", ["-c:a", "adpcm_ima_wav"]),
    ("wav_mulaw_8k.wav", "WAV mu-law 8kHz", ["-ar", "8000", "-ac", "1", "-c:a", "pcm_mulaw"]),
    ("aiff_16_44.aiff", "AIFF 16/44", ["-c:a", "pcm_s16be"]),
    ("au_16_44.au", "AU 16/44", ["-c:a", "pcm_s16be"]),
    ("wavpack_16_44.wv", "WavPack 16/44", ["-c:a", "wavpack", "-frame_size", "22050"]),
    ("wavpack_16_44_hi.wv", "WavPack 16/44 high",
     ["-c:a", "wavpack", "-frame_size", "22050", "-compression_level", "3"]),
    ("tta_16_44.tta", "TTA 16/44", ["-c:a", "tta"]),
    ("ac3_192.ac3", "AC3 192k", ["-c:a", "ac3", "-b:a", "192k"]),
    ("ac3_448.ac3", "AC3 448k", ["-c:a", "ac3", "-b:a", "448k"]),
    ("speex_wb.spx", "Speex 16kHz", ["-ar", "16000", "-ac", "1", "-c:a", "libspeex"]),
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


def smaf_audio_track(path: Path, title: str) -> None:
    """Makes ffmpeg's SMAF file one Rockbox plays. Its parsers
    (lib/rbcodec/metadata/smaf.c, codecs/smaf.c) take a Contents Info chunk
    of 5 bytes to mean a score track, and they then want a Data Chunk in
    OPDA and an MTR. ffmpeg writes those 5 bytes before an audio track
    (ATR), so playback skipped the file. A title tag (ST:...,) in CNTI
    makes the chunk longer, and the parsers take the audio-track path."""
    raw = bytearray(path.read_bytes())
    if raw[:4] != b"MMMD" or raw[8:12] != b"CNTI":
        raise MediaError(f"{path}: not a SMAF file")
    size = int.from_bytes(raw[12:16], "big")
    tag = f"ST:{title},".encode()
    raw[16 + size:16 + size] = tag
    raw[12:16] = (size + len(tag)).to_bytes(4, "big")
    raw[4:8] = (int.from_bytes(raw[4:8], "big") + len(tag)).to_bytes(4, "big")
    path.write_bytes(raw)


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
            if p.suffix == ".mmf":
                smaf_audio_track(p, name)
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


class MediaError(ArbError):
    pass


def load(out: Path) -> list[Entry]:
    """The entries of the manifest that `make` wrote into OUT"""
    f = out / "manifest.json"
    try:
        data = json.loads(f.read_text())
    except FileNotFoundError:
        raise MediaError(f"no {f}: make the set with `arb media {out}` and copy it to the "
                         "card") from None
    return [Entry(**e) for e in data["files"]]


def codectest(out: Path, tone: Path, *,
              echo: Callable[[str], object] = print) -> list[Path]:
    """Writes the torture's codec set from TONE: OUT/codectest/, a
    CODECTEST_S clip per CODECTEST case; returns the clips"""
    if not tone.is_file():
        raise FileNotFoundError(f"no tone {tone}")
    folder = out / CODECTEST_DIR
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    for name, args in CODECTEST:
        clip = folder / name
        _encode(["-i", str(tone)],
                ["-t", str(CODECTEST_S), "-map", "0:a", "-map_metadata", "-1", *args], clip)
        clips.append(clip)
        echo(f"{clip.stat().st_size // 1024:6d} KB  {name}")
    return clips


def stress(out: Path, source: Path, *, start: float = 0.0,
           echo: Callable[[str], object] = print) -> list[Path]:
    """Writes the stress set from SOURCE (its clips start START s into it):
    OUT/stress/ and OUT/STRESS_PLAYLIST; returns the clips"""
    if not source.is_file():
        raise FileNotFoundError(f"no stress source {source}")
    folder = out / "stress"
    folder.mkdir(parents=True, exist_ok=True)
    clips = []
    for name, title, args in STRESS:
        clip = folder / name
        _encode(["-ss", str(start), "-i", str(source)],
                ["-t", str(STRESS_S), "-map", "0:a", "-map_metadata", "-1",
                 "-metadata", f"title={title}", "-metadata", f"artist=Stress: {source.stem}",
                 "-metadata", "album=Codec stress set", *args], clip)
        clips.append(clip)
        echo(f"{clip.stat().st_size // 1024:6d} KB  {title}")
    playlist = out / STRESS_PLAYLIST
    playlist.parent.mkdir(exist_ok=True)
    playlist.write_text("".join(f"/{folder.name}/{c.name}\n" for c in clips))
    return clips
