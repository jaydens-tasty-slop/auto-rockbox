"""`arb media`: the codec and video test media; `arb media-stress`: the
codec stress set (auto_rockbox.media)."""

from __future__ import annotations

import subprocess
import sys
from argparse import Namespace
from pathlib import Path

from .. import media
from ..registry import Context, arg, command


@command("media", help="make codec and video test media from a tone file", target=False, args=[
    arg("out", type=Path, help="output directory: codecs/, video/, manifest.json"),
    arg("--tone", type=Path, help="the tone (default [media] tone, build/audio/tone.wav)"),
    arg("--extra", type=Path, metavar="DIR", help="clips to copy (default [media] extra)"),
    arg("--mod", type=Path, metavar="FILE", help="a tracker module (default [media] mod)"),
    arg("--video", type=Path, nargs="*", metavar="FILE",
        help="videos to copy (default [media] videos)"),
])
def media_cmd(ctx: Context, args: Namespace) -> int:
    """Encodes the tone into a 12 s clip per codec and four test videos,
    copies the extra clips, module and videos, and writes OUT/manifest.json
    (see auto_rockbox.media). Copy codecs/ and video/ to the card."""
    conf = ctx.ws.section("media")
    videos = args.video if args.video is not None else [
        ctx.ws.root / v for v in conf.get("videos", [])]
    try:
        media.make(args.out, args.tone or conf.path("tone", "build/audio/tone.wav"),
                   extra=args.extra or conf.opt_path("extra"),
                   mod=args.mod or conf.opt_path("mod"), videos=videos,
                   video_size=conf.string("video_size", "160x128"))
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"arb media: {e}", file=sys.stderr)
        return 1
    print(args.out / "manifest.json")
    return 0


@command("media-stress", help="make the codec stress set: a music clip per codec case",
         target=False, args=[
             arg("out", type=Path,
                 help="output directory: stress/ and Playlists/, for the card root"),
             arg("--source", type=Path, metavar="FILE",
                 help="music to encode (default [media] stress_source, "
                      "build/audio/codecs/flac_16_44.flac); a real song makes glitches easy "
                      "to hear"),
             arg("--start", type=float, default=0.0, metavar="S",
                 help="seconds into the source where each clip starts (default 0)"),
         ])
def media_stress(ctx: Context, args: Namespace) -> int:
    """Encodes a 30 s clip of the source per codec, bitrate and rate case,
    each titled by its case, and a playlist of them all, Playlists/Stress
    test.m3u8 (see auto_rockbox.media). Copy OUT's contents to the card
    root, e.g. `arb yp3 card-run 'cp -r OUT/. "$MP/"'`."""
    source = args.source or ctx.ws.section("media").path(
        "stress_source", "build/audio/codecs/flac_16_44.flac")
    try:
        clips = media.stress(args.out, source, start=args.start)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"arb media-stress: {e}", file=sys.stderr)
        return 1
    print(f"{len(clips)} clips -> {args.out}")
    return 0
