"""`arb audio`: a player's analog output against a reference device, through
the same Line In (auto_rockbox.audioq)."""

from __future__ import annotations

import json
import sys
from argparse import ArgumentTypeError, Namespace
from pathlib import Path

import numpy as np

from .. import audioq
from ..audioq import plots
from ..audioq import selftest as selftest_run
from ..audioq import slips as slip_finder
from ..audioq.analyze import Result
from ..audioq.audiofile import AudioError, flac_encode, load_capture, read_audio, write_wav
from ..audioq.eq import fit_preset
from ..audioq.pipeline import analyse_to, volsteps_to
from ..audioq.signal import SIGNAL_NAME, load_timeline, write_signal
from ..linein import Second
from ..registry import CommandFn, Context, arg, command, group
from .linein import WINDOW_ARGS, selected


def _out(ctx: Context) -> Path:
    return audioq.out_dir(ctx.ws)


def _floats(text: str) -> list[float]:
    try:
        return [float(v) for v in text.split(",")]
    except ValueError:
        raise ArgumentTypeError(f"{text}: numbers separated by commas") from None


def _labelled(spec: str) -> tuple[str, Path]:
    label, sep, path = spec.partition("=")
    if not sep:
        raise ArgumentTypeError(f"{spec}: LABEL=PATH")
    return label, Path(path)


def _load(spec: tuple[str, Path]) -> Result:
    r = json.loads(spec[1].read_text())
    r["label"] = spec[0]
    return r


def _recording(ctx: Context, args: Namespace) -> tuple[np.ndarray, int, list[Second]]:
    """--wav, or the Line In seconds the window names (and those seconds)"""
    if args.wav:
        x, fs = read_audio(args.wav)
        return x, fs, []
    seconds = selected(ctx, args)
    x, fs = load_capture(seconds)
    return x, fs, seconds


def signal(ctx: Context, args: Namespace) -> int:
    """Writes the test signal: test_signal_16.flac and _24.flac for the
    reference device, _16.wav, the timeline test_signal.json the analysis
    reads, and volsteps_16.flac/.wav (1 kHz at -3 dBFS for 120 s, for the
    volume-step run). Play the 16-bit FLAC losslessly on both devices."""
    out = args.out or _out(ctx) / "signal"
    tl = write_signal(out)
    print(f"{tl.total:.1f} s, {len(tl.segments)} segments -> {out}")
    for s in tl.segments:
        print(f"  {s.start:7.2f} {s.dur:5.1f}  {s.name}")
    return 0


def analyze(ctx: Context, args: Namespace) -> int:
    """Analyses one pass of the test signal: from the Line In ring (the
    window must hold exactly one pass; it is saved as capture.flac) or any
    WAV/FLAC. Writes OUT/LABEL/result.json, report.txt and graphs. With
    --volsteps: the plateaus of a volume-step run (volsteps.json/.png)."""
    out = args.out or _out(ctx) / args.label
    x, fs, seconds = _recording(ctx, args)
    source = str(args.wav) if args.wav else f"capture {seconds[0]} .. {seconds[-1]}"
    if seconds:
        out.mkdir(parents=True, exist_ok=True)
        wav = out / "capture.wav"
        write_wav(wav, x, fs, 16, dither=False)
        try:
            flac_encode(wav, out / "capture.flac", keep_wav=False)
        except AudioError as e:
            print(f"(capture kept as WAV: {e})")
    if x.shape[1] == 1:
        x = np.repeat(x, 2, 1)
    if args.volsteps:
        volsteps_to(x, fs, args.label, out, volumes=args.volumes, source=source)
    else:
        tl = load_timeline(args.timeline or _out(ctx) / "signal" / f"{SIGNAL_NAME}.json")
        analyse_to(x, fs, tl, args.label, out, source=source)
    print(f"-> {out}")
    return 0


def compare(ctx: Context, args: Namespace) -> int:
    """Overlays results (before/after graphs). The first is the reference.
    Writes fr.png (1/6-octave response normalised at 1 kHz, and each minus
    the reference), thdn_level.png, hd_freq.png and, with --volsteps,
    volsteps.png. --predict LABEL=preset.cfg adds that result with the preset
    applied, to check the prediction against an "after" run."""
    plots.require()
    args.out.mkdir(parents=True, exist_ok=True)
    if args.volsteps:
        plots.volsteps([_load(s) for s in args.volsteps], args.out / "volsteps.png")
    if args.inputs:
        plots.all_of([_load(s) for s in args.inputs], args.out, args.channel, args.sweep,
                     args.predict)
    print(f"-> {args.out}")
    return 0


def fit_eq(ctx: Context, args: Namespace) -> int:
    """Fits Rockbox's 10-band EQ so the device matches the reference:
    target = reference - device response (1/6 octave, normalised at 1 kHz).
    Prints the bands, the predicted residual and the precut."""
    if args.plot:
        plots.require()
    fit = fit_preset(json.loads(args.ref.read_text()), json.loads(args.dev.read_text()),
                     channel=args.channel, max_boost=args.max_boost, fmin=args.fmin,
                     fmax=args.fmax)
    out = args.out or _out(ctx) / "preset.cfg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(fit.cfg())
    print("".join(fit.cfg().splitlines(keepends=True)[2:]), end="")
    before, resid = fit.before, fit.residual
    print(f"target (device error) rms {before[0]:.2f} dB, max {before[1]:.2f} dB")
    print(f"predicted residual rms {resid[0]:.2f} dB, max {resid[1]:.2f} dB "
          f"({fit.fmin:g}-{fit.fmax:g} Hz)")
    print(f"preset peak boost {fit.peak:+.2f} dB -> precut {fit.precut / 10:.1f} dB; "
          f"{sum(b[3] != 0 for b in fit.bands)} bands active -> {out}")
    if args.plot:
        plots.fit(fit, args.plot)
    return 0


def slips(ctx: Context, args: Namespace) -> int:
    """Whole-sample slips (clicks) in a steady tone: phase steps of the tone
    over 10 ms. Times are seconds into the recording, and the Line In second
    they fall in."""
    x, fs, seconds = _recording(ctx, args)
    s = slip_finder.find(x[:, 0], fs, args.f0)
    where = str(args.wav) if args.wav else str(seconds[0])
    if abs(s.hz - args.f0) > 0.03 * args.f0:
        print(f"no steady {args.f0:g} Hz tone in {where} (strongest {s.hz:.1f} Hz)",
              file=sys.stderr)
        return 1
    print(f"{s.seconds:.1f} s steady from {s.start:.1f} s into {where}, f {s.hz:.3f} Hz; "
          f"{len(s.events)} slips, net {s.net:+.1f} samples")
    for t, v in s.events:
        sec = f" ({seconds[int(t)]} +{t % 1:.3f})" if seconds else ""
        print(f"  {t:8.3f} s{sec}  {v:+.2f} samples")
    return 0


def selftest(ctx: Context, args: Namespace) -> int:
    """End-to-end check without a device: simulates a reference and a
    device (response, distortion, noise, hum, crosstalk, clock offsets),
    analyses both, fits a preset, simulates the "after" run and compares.
    Exit status 1 if what it recovers is off the models' truth."""
    out = args.out or _out(ctx) / "selftest"
    o = selftest_run.run(out)
    print(f"\nrecovered device response vs true, 25 Hz-18 kHz: max err {o.err_max:.2f} dB, "
          f"rms {o.err_rms:.2f} dB")
    print(f"after EQ vs reference: rms {o.after_rms:.2f} dB, max {o.after_max:.2f} dB "
          f"(before: rms {o.before_rms:.2f}, max {o.before_max:.2f})")
    print(f"preset: {sum(b[3] != 0 for b in o.fit.bands)} bands, precut {o.fit.precut / 10:.1f} "
          f"dB, predicted residual rms {o.fit.residual[0]:.2f} dB")
    for label in (selftest_run.REF, selftest_run.BEFORE):
        r = o.results[label]
        print(f"{label}: clock {r['clock_ppm']:+.1f} ppm, gain 1k {r['gain_1k'][0]:+.2f} dB, "
              f"sync quality {r['sync_quality']:.2f}")
    print(f"H2/H3 at -20 dBFS in: expected {o.h_expected[0]:.1f} / {o.h_expected[1]:.1f} dB, "
          f"measured {o.h_measured[0]:.1f} / {o.h_measured[1]:.1f} dB")
    print(f"-> {out}")
    for p in o.problems:
        print(f"FAIL: {p}", file=sys.stderr)
    return 1 if o.problems else 0


def _guarded(fn: CommandFn) -> CommandFn:
    """Missing tools (flac, matplotlib) and empty windows end with a message"""
    def run(ctx: Context, args: Namespace) -> int | None:
        try:
            return fn(ctx, args)
        except (AudioError, plots.PlotsUnavailable) as e:
            print(f"arb audio: {e}", file=sys.stderr)
            return 1
    run.__doc__ = fn.__doc__
    return run


_SOURCE = [
    *WINDOW_ARGS,
    arg("--wav", type=Path, metavar="FILE", help="a WAV or FLAC recording instead"),
]

group("audio", help="audio quality against a reference device: signal, analysis, EQ fit")

command("audio signal", target=False, help="write the test signal", args=[
    arg("out", type=Path, nargs="?", help="directory (default OUT/signal)"),
])(_guarded(signal))

command("audio analyze", target=False, help="analyse one pass of the signal", args=[
    arg("label", help="the run's name, e.g. reference, before, after"),
    *_SOURCE,
    arg("--timeline", type=Path, help="the signal's test_signal.json "
        "(default OUT/signal/test_signal.json, else this version's)"),
    arg("-o", "--out", type=Path, help="output directory (default OUT/LABEL)"),
    arg("--volsteps", action="store_true", help="a volume-step run (signal volsteps_16)"),
    arg("--volumes", type=_floats, metavar="V,V,...",
        help="--volsteps: the volume settings, labelling the plateaus in order"),
])(_guarded(analyze))

command("audio compare", target=False, help="overlay results in graphs", args=[
    arg("inputs", nargs="*", type=_labelled, metavar="LABEL=result.json"),
    arg("-o", "--out", type=Path, required=True),
    arg("--predict", type=_labelled, metavar="LABEL=preset.cfg"),
    arg("--channel", default="avg", choices=["avg", "L", "R"]),
    arg("--sweep", default="12", choices=["12", "3"], help="the sweep's level, -dBFS"),
    arg("--volsteps", nargs="*", type=_labelled, metavar="LABEL=volsteps.json"),
])(_guarded(compare))

command("audio fit-eq", target=False, help="fit an EQ preset to match the reference", args=[
    arg("ref", type=Path, metavar="REF.json"),
    arg("dev", type=Path, metavar="DEV.json"),
    arg("-o", "--out", type=Path, help="the preset (default OUT/preset.cfg)"),
    arg("--max-boost", type=float, default=6.0, help="dB (default 6)"),
    arg("--fmin", type=float, default=25.0, help="Hz (default 25)"),
    arg("--fmax", type=float, default=18000.0, help="Hz (default 18000)"),
    arg("--channel", default="avg", choices=["avg", "L", "R"]),
    arg("--plot", type=Path, metavar="PNG", help="also draw the fit"),
])(_guarded(fit_eq))

command("audio slips", target=False, help="find sample slips (clicks) in a steady tone", args=[
    *_SOURCE,
    arg("--f0", type=float, default=1000.0,
        help="the tone's frequency at the player (default 1000 Hz)"),
])(_guarded(slips))

command("audio selftest", target=False,
        help="simulate a reference and a device, check it all",
        args=[arg("-o", "--out", type=Path, help="output directory (default OUT/selftest)")],
        )(_guarded(selftest))

