"""End-to-end check without a device or a capture.

Simulates a reference player (near-flat) and a device (35 Hz coupling-cap
high-pass, +3 dB at 3 kHz, -5 dB shelf at 9 kHz, 20 kHz roll-off, tanh soft
clip plus some H2, -55 dB crosstalk, noise and hum), each with its own clock
offset and lead-in, recorded at 48 kHz/16 bit. Analyses both, fits a preset,
plays the preset through the device model (the player's DSP: precut, EQ,
16-bit clip) as "after", draws the comparison with the predicted curve, and
runs a volume-step pair. What the analysis recovers is checked against the
models' known truth.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import plots
from .analyze import Result, fr_curve
from .audiofile import read_audio, write_wav
from .dsp import norm_at
from .eq import HS, PK, Fit, biquad, biquad_resp, eq_response, fit_preset, read_cfg
from .pipeline import Echo, analyse_to, volsteps_to
from .signal import GEN_FS, Segment, synth, synth_segment, timeline

CAP_FS = 48000
DEVICE_GAIN_DB = -8.0
REF, BEFORE, AFTER = "reference (sim)", "device before", "device after"

Response = Callable[[np.ndarray], np.ndarray]


def resample(x: np.ndarray, n_out: int) -> np.ndarray:
    """FFT resampling along axis 0 (band-limited, circular)"""
    spec = np.fft.rfft(x, axis=0)
    m = n_out // 2 + 1
    out = np.zeros((m, *spec.shape[1:]), complex)
    k = min(m, spec.shape[0])
    out[:k] = spec[:k]
    return np.fft.irfft(out, n_out, axis=0) * (n_out / len(x))


def dev_linear(f: np.ndarray) -> np.ndarray:
    h = (1j * f / 35) / (1 + 1j * f / 35)
    h = h * biquad_resp(*biquad(PK, 3000, 15, 30, CAP_FS), f, CAP_FS)
    h = h * biquad_resp(*biquad(HS, 9000, 7, -50, CAP_FS), f, CAP_FS)
    return h / (1 - (f / 20000) ** 2 + 1j * np.sqrt(2) * f / 20000)


def ref_linear(f: np.ndarray) -> np.ndarray:
    h = (1j * f / 5) / (1 + 1j * f / 5)
    return h / (1 - (f / 21500) ** 2 + 1j * np.sqrt(2) * f / 21500)


def filt(x: np.ndarray, h: Response, fs: float) -> np.ndarray:
    f = np.fft.rfftfreq(len(x), 1 / fs)
    return np.fft.irfft(np.fft.rfft(x, axis=0) * h(f)[:, None], len(x), axis=0)


def dev_nl(gain: float = 1.0) -> Response:
    return lambda y: 0.9 * (np.tanh(gain * y) + 0.01 * (gain * y) ** 2)


def chain(x44: np.ndarray, h: Response, *, ppm: float, gain_db: float, nl: Response,
          noise_db: float, xt_db: float, seed: int, lead: float = 3.37) -> np.ndarray:
    """A player at 44.1 kHz with a clock error of PPM, its analog stage,
    and a 48 kHz capture"""
    n_out = round(len(x44) * CAP_FS / GEN_FS / (1 + ppm * 1e-6))
    y = nl(filt(resample(x44, n_out), h, CAP_FS) * 10 ** (gain_db / 20))
    y[:, 1] += y[:, 0] * 10 ** (xt_db / 20)
    y[:, 0] += y[:, 1] * 10 ** (xt_db / 20)
    r = np.random.default_rng(seed)
    y = np.concatenate([np.zeros((int(lead * CAP_FS), 2)), y, np.zeros((2 * CAP_FS, 2))])
    t = np.arange(len(y)) / CAP_FS
    y += r.normal(0, 10 ** (noise_db / 20) / np.sqrt(2), y.shape)
    y += (10 ** ((noise_db - 6) / 20) * np.sin(2 * np.pi * 50 * t))[:, None]
    return y


def device(x44: np.ndarray, seed: int, lead: float = 3.37, gain: float = 1.0) -> np.ndarray:
    return chain(x44, dev_linear, ppm=80, gain_db=DEVICE_GAIN_DB, nl=dev_nl(gain),
                 noise_db=-95, xt_db=-55, seed=seed, lead=lead)


@dataclass(frozen=True)
class Outcome:
    err_rms: float              # recovered device response against the truth, dB
    err_max: float
    before_rms: float           # the device against the reference, no EQ
    before_max: float
    after_rms: float            # ... with the fitted preset
    after_max: float
    fit: Fit
    h_expected: tuple[float, float]     # H2, H3 at -20 dBFS in, from the model
    h_measured: tuple[float, float]
    results: dict[str, Result]

    @property
    def problems(self) -> list[str]:
        """Where the analysis missed the models' truth by more than it
        should (it did 0.46 dB max, 0.17 rms; 0.43 max after EQ; H2/H3 0.2)"""
        out = []
        if self.err_max > 1.0 or self.err_rms > 0.3:
            out.append(f"response recovered within {self.err_max:.2f} dB max, "
                       f"{self.err_rms:.2f} rms")
        if self.after_max > 1.0 or self.after_rms > 0.3:
            out.append(f"after EQ {self.after_max:.2f} dB max, {self.after_rms:.2f} rms")
        for n, e, m in zip((2, 3), self.h_expected, self.h_measured, strict=True):
            if abs(e - m) > 0.5:
                out.append(f"H{n} {m:.1f} dB, the model gives {e:.1f}")
        return out


def _rms_max(y: np.ndarray) -> tuple[float, float]:
    return float(np.sqrt((y ** 2).mean())), float(np.abs(y).max())


def run(out: Path, echo: Echo = print) -> Outcome:
    out.mkdir(parents=True, exist_ok=True)
    tl = timeline()
    x = synth(GEN_FS, tl)
    results: dict[str, Result] = {}

    def capture(name: str, y: np.ndarray, label: str) -> None:
        wav = out / f"{name}.wav"
        write_wav(wav, y, CAP_FS)
        echo(f"--- {label}")
        xc, fs = read_audio(wav)
        results[label] = analyse_to(xc, fs, tl, label, out / name, source=str(wav), echo=echo)

    capture("ref", chain(x, ref_linear, ppm=-30, gain_db=-3, nl=lambda y: y + 1e-4 * y ** 3,
                         noise_db=-105, xt_db=-90, seed=1), REF)
    capture("before", device(x, 2, lead=5.81), BEFORE)
    fit = fit_preset(results[REF], results[BEFORE])
    cfg = out / "selftest.cfg"
    cfg.write_text(fit.cfg())
    preset = read_cfg(cfg)
    xe = filt(x, lambda f: eq_response(preset.bands, f, GEN_FS, preset.precut), GEN_FS)
    capture("after", device(np.clip(xe, -1, 1), 3, lead=1.2), AFTER)

    echo("--- volume steps, two hypothetical analog gains (digital -30..0 dB)")
    vols = list(range(-30, 1, 3))
    tone = synth_segment(Segment("tone", "tone", 0.0, 3.0, freq=1000.0, dbfs=-3.0), GEN_FS)
    steps = []
    for name, gain in (("analog_0dB", 1.0), ("analog_+6dB", 2.0)):
        seq = np.concatenate([tone * 10 ** (v / 20) for v in vols])
        wav = out / f"{name}.wav"
        write_wav(wav, device(np.stack([seq, seq], 1), 4, gain=gain), CAP_FS)
        xc, fs = read_audio(wav)
        steps.append(volsteps_to(xc, fs, name, out / name, volumes=vols, source=str(wav),
                                 echo=echo))
    try:
        plots.fit(fit, out / "fit.png")
        plots.all_of(list(results.values()), out, predict=(BEFORE, cfg))
        plots.volsteps(steps, out / "volsteps.png")
    except plots.PlotsUnavailable as e:
        echo(f"(no graphs: {e})")

    g = np.array(results[REF]["grid"])
    m = (g >= 25) & (g <= 18000)
    true = norm_at(g, 20 * np.log10(np.abs(dev_linear(g) / ref_linear(g))))
    ref = fr_curve(results[REF])[1]
    err = _rms_max((fr_curve(results[BEFORE])[1] - ref - true)[m])
    before = _rms_max(true[m])
    after = _rms_max((fr_curve(results[AFTER])[1] - ref)[m])
    # tanh(x) + 0.01 x^2 at the device's -8 dB, -20 dBFS in: H2 = 0.005 a, H3 = a^2 / 12
    a = 10 ** ((-20 + DEVICE_GAIN_DB) / 20)
    h = results[BEFORE]["steps"][2]["ch"][0]["h"]
    return Outcome(*err, *before, *after, fit,
                   (float(20 * np.log10(0.005 * a)), float(20 * np.log10(a * a / 12))),
                   (h[0], h[1]), results)
