"""Test media: step-down detection, the OKI ADPCM encoder and spectrum
classification (the last with ffmpeg, if present); the stress set's cases
(against yp3dev/stress_set.py where the port's checkout has it), its command
and, with ffmpeg, the set itself in 1 s clips."""

from __future__ import annotations

import contextlib
import importlib
import io
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

import auto_rockbox.commands.media  # noqa: F401  (registers `arb media-*`)
from auto_rockbox import cli, linein, media
from auto_rockbox.registry import Context
from auto_rockbox.suites import rockbox_torture as rt
from auto_rockbox.workspace import Workspace

RATE = media.RATE


def tone(left: float, right: float, seconds: float, db: float = -10.0) -> np.ndarray:
    t = np.arange(int(seconds * RATE)) / RATE
    amp = 10 ** (db / 20)
    return amp * np.stack([np.sin(2 * np.pi * left * t), np.sin(2 * np.pi * right * t)], axis=1)


def int16(a: np.ndarray) -> np.ndarray:
    return (a * 32767).astype(np.int16)


def write_wav(path: Path, a: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(a.shape[1])
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(int16(a).tobytes())
    return path


def oki_decode(data: bytes) -> np.ndarray:
    """The decoder side of media.oki_adpcm, to check the encoder against"""
    pred, idx, out = 0, 0, []
    for byte in data:
        for code in (byte >> 4, byte & 15):
            step = media.OKI_STEPS[idx]
            delta = step >> 3
            for bit, part in ((4, step), (2, step >> 1), (1, step >> 2)):
                if code & bit:
                    delta += part
            pred = max(-2048, min(2047, pred - delta if code & 8 else pred + delta))
            idx = max(0, min(48, idx + media.OKI_ADJUST[code & 7]))
            out.append(pred << 4)
    return np.array(out, float) / 32768


class TestAnalysis(unittest.TestCase):
    def test_step_down(self) -> None:
        stepped = np.concatenate([tone(1000, 1500, 3), tone(1000, 1500, 2, -22),
                                  np.zeros((RATE, 2))])
        assert media.step_down(int16(stepped)) == 3.0
        assert media.step_down(int16(tone(1000, 1500, 4))) is None
        to_silence = np.concatenate([tone(1000, 1500, 3), np.zeros((2 * RATE, 2))])
        assert media.step_down(int16(to_silence)) is None

    def test_oki_adpcm(self) -> None:
        x = int16(tone(1000, 1000, 1)[:, 0])[::6]           # 8 kHz
        data = media.oki_adpcm(x)
        assert len(data) == len(x) // 2
        y = oki_decode(data)
        assert abs(linein.dominant_hz(y, media.VOX_RATE) - 1000) < 2
        assert np.corrcoef(y[800:], x[800:])[0, 1] > 0.98

    @unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg")
    def test_classify(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            assert media.classify(write_wav(root / "s.wav", tone(1000, 1500, 5))) == "stereo"
            assert media.classify(write_wav(root / "m.wav", tone(1000, 1000, 5))) == "tone"
            assert media.classify(write_wav(root / "a.wav", tone(440, 660, 5))) == "music"
            (root / "x.vox").write_bytes(bytes(range(256)) * 64)
            (root / "x.bin").write_bytes(bytes(range(256)) * 64)
            assert media.classify(root / "x.vox") == "tone"
            assert media.classify(root / "x.bin") == "unknown"
            e = media.describe(root, write_wav(
                root / "t.wav", np.concatenate([tone(1000, 1500, 4), tone(1000, 1500, 2, -22)])))
            assert e == media.Entry("t.wav", "stereo", 6.0, 4.0), e


    @unittest.skipUnless(shutil.which("ffmpeg"), "no ffmpeg")
    def test_smaf_audio_track(self) -> None:
        """ffmpeg's SMAF file, rewritten, takes Rockbox's audio-track path (a
        Contents Info chunk over 5 bytes) and its chunk search finds the
        audio track and its wave data; ffmpeg still decodes it the same"""
        args = dict(media.CODECS)["smaf.mmf"]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = write_wav(root / "t.wav", tone(1000, 1500, 2))
            mmf = root / "t.mmf"
            subprocess.run(["ffmpeg", "-v", "error", "-i", str(src), *args, str(mmf)], check=True)
            before = media.decode(mmf)
            media.smaf_audio_track(mmf, "smaf.mmf")
            raw = mmf.read_bytes()
            cnti = int.from_bytes(raw[12:16], "big")
            assert cnti > 5 and raw[16 + cnti - 12:16 + cnti] == b"ST:smaf.mmf,"
            assert int.from_bytes(raw[4:8], "big") == len(raw) - 8
            pos, found = 16 + cnti, {}
            while pos + 8 <= len(raw):      # search_chunk(), metadata/smaf.c
                name, size = raw[pos:pos + 4], int.from_bytes(raw[pos + 4:pos + 8], "big")
                found[name[:3]] = (pos + 8, size)
                pos += 8 + size
                if name[:3] == b"ATR":
                    pos = found[b"ATR"][0] + 6          # its format bytes, then its chunks
            assert b"ATR" in found and b"Awa" in found, found
            assert np.array_equal(media.decode(mmf), before)
            with self.assertRaises(media.MediaError):
                media.smaf_audio_track(src, "x")


PORT = Path(__file__).resolve().parents[3]          # rockbox-yp3, if auto-rockbox sits in it
# The extensions Rockbox picks the stress set's codecs by
PLAYABLE = {".mp3", ".mp2", ".m4a", ".ogg", ".opus", ".flac", ".wav", ".aiff", ".au", ".wv",
            ".tta", ".ac3", ".spx"}


def tags(path: Path) -> dict[str, str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags",
                          "-of", "default=nw=1", str(path)], capture_output=True, text=True,
                         check=True).stdout
    return {k.removeprefix("TAG:").lower(): v for k, _, v in
            (line.partition("=") for line in out.splitlines())}


class TestStress(unittest.TestCase):
    def test_cases(self) -> None:
        names = [n for n, _, _ in media.STRESS]
        assert len(set(names)) == len(names) == 41
        assert len({t for _, t, _ in media.STRESS}) == 41
        for name, _, args in media.STRESS:
            assert Path(name).suffix in PLAYABLE, name
            assert "-c:a" in args, name
            if Path(name).suffix in (".ogg", ".opus"):
                assert args[-2:] == media.OGG_PAGES, name     # not ffmpeg's 1 s pages
            if Path(name).suffix == ".flac":
                assert "-frame_size" in args, name            # not the source's block size

    @unittest.skipUnless((PORT / "yp3dev/stress_set.py").exists(), "no yp3dev/stress_set.py")
    def test_cases_as_yp3dev(self) -> None:
        sys.path.insert(0, str(PORT / "yp3dev"))
        try:                # its bytecode is not written there
            with patch.object(sys, "dont_write_bytecode", True):
                old = importlib.import_module("stress_set")
        finally:
            sys.path.remove(str(PORT / "yp3dev"))
        cases = [(f"{stem}.{ext}", title, args) for stem, title, args, ext in old.CASES]
        assert cases == media.STRESS
        assert media.STRESS_S == old.SECONDS

    def test_command_without_source(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            ws = Workspace(Path(d), {"media": {"stress_source": "song.flac"}})
            args = cli.parser(ws).parse_args(["media-stress", str(Path(d, "out"))])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                assert args._command.fn(Context(ws), args) == 1
            assert f"no stress source {Path(d, 'song.flac')}" in err.getvalue(), err.getvalue()

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "no ffmpeg")
    def test_codectest(self) -> None:
        with tempfile.TemporaryDirectory() as d, patch.object(media, "CODECTEST_S", 1):
            root = Path(d)
            clips = media.codectest(root / "out", write_wav(root / "tone.wav", tone(1000, 1500, 3)),
                                    echo=lambda _: None)
            assert [c.name for c in clips] == [n for n, _ in media.CODECTEST]
            assert all(c.parent == root / "out" / media.CODECTEST_DIR for c in clips)
            assert media.duration(root / "out/codectest/wav_24_96.wav") == 1.0
        # the files the torture names are in the set
        names = {f"/{media.CODECTEST_DIR}/{n}" for n, _ in media.CODECTEST}
        assert {rt.PROBE, rt.UNSUPPORTED} <= names

    def test_stress(self) -> None:
        with tempfile.TemporaryDirectory() as d, patch.object(media, "STRESS_S", 1):
            root = Path(d)
            song = write_wav(root / "song.wav", tone(440, 660, 3))
            printed: list[str] = []
            clips = media.stress(root / "out", song, start=0.5, echo=printed.append)
            assert [c.name for c in clips] == [n for n, _, _ in media.STRESS]
            assert all(c.stat().st_size > 0 for c in clips) and len(printed) == 41
            playlist = (root / "out" / media.STRESS_PLAYLIST).read_text().splitlines()
            assert playlist == [f"/stress/{n}" for n, _, _ in media.STRESS], playlist
            mp3 = root / "out/stress/mp3_cbr128.mp3"
            mine = {"title": "MP3 CBR 128k", "artist": "Stress: song", "album": "Codec stress set"}
            assert mine.items() <= tags(mp3).items(), tags(mp3)      # and ffmpeg's encoder
            assert media.duration(root / "out/stress/wav_16_44.wav") == 1.0


if __name__ == "__main__":
    unittest.main()
