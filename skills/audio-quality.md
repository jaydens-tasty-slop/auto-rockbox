# Audio quality

Measure the player's analog output against a **known-good reference device**
(a phone with a clean headphone output: the YP3 used an iPhone with Apple's
3.5 mm adapter) through the **same** PC Line In. The capture's own response
and noise then cancel in the difference. This gives the frequency response,
distortion against level, noise, crosstalk and the clean maximum level. When
the response is not flat, it also gives an EQ preset that makes it match the
reference.

## Signal

`arb audio signal build/audio/signal` writes the test signal (154 s, 44.1 kHz
stereo, as 16-bit FLAC, 24-bit FLAC and WAV) and its timeline. In order:

- an up-chirp for sync,
- 1 kHz at -20 dBFS,
- 8 s of digital silence (noise),
- 1 kHz steps from -60 to 0 dBFS (THD+N against level),
- exponential sweeps from 20 Hz to 20 kHz at -12 and -3 dBFS, left then
  right (response, harmonics, crosstalk),
- spot tones,
- CCIF and SMPTE intermodulation,
- a down-chirp at the end.

The chirps locate every segment and the clock offset between player and PC,
so no timestamps are needed. A separate 1 kHz file at -3 dBFS serves
volume-step runs and the click finder.

## Conditions

These made the difference between real results and artefacts on the YP3:

- **Line In at unity gain**, always. At 100% the capture adds 60 dB and
  everything looks clipped.
- **Measure the player on battery.** On USB, the YP3's output clipped about
  7 dB early into Line In (a ground loop or the USB-attached state), and
  its distortion figures looked far worse than stock's. Set up over USB,
  then unplug for the pass.
- **A real load**: the same headphones on a splitter with Line In, for every
  device. Crosstalk and the clip point depend on the load (the YP3 had
  -31 dB crosstalk loaded and -52 dB unloaded).
- **The reference**: EQ, loudness normalisation and headphone accommodation
  off, Do Not Disturb on. Use the highest volume step where the 0 dBFS step
  does not clip the capture (watch `clipped_samples` in the capture's
  JSON). Some phones limit their input above -3 dBFS and ramp the level up
  over the first seconds of a file. The analyser takes the clock from every
  chirp and sweep because of that.
- **The player**: EQ, tone controls, ReplayGain and crossfade off; volume at
  its maximum, or at a noted step.
- One pass per analysis window: two start chirps in the window make the cut
  miss.

## Procedure

```
arb linein serve --history 600                 # keeps the last 600 s
# play test_signal_16.flac on the reference, once
arb audio analyze reference 200                # the last 200 s
# copy the signal to the player's card (arb TARGET card push), play it on battery
arb audio analyze before 12:01:00 12:04:00     # or a UTC window
arb audio fit-eq build/audio/reference/result.json build/audio/before/result.json \
    -o build/audio/headphones.cfg --plot build/audio/fit.png
# load the preset on the player (settings .cfg), same volume, play again
arb audio analyze after ...
arb audio compare -o build/audio/cmp Reference=build/audio/reference/result.json \
    Before=build/audio/before/result.json After=build/audio/after/result.json \
    --predict Before=build/audio/headphones.cfg
```

`analyze` writes `report.txt` (gain, balance, polarity, noise flat and
A-weighted, THD+N per step, clip point, dynamic range, IMD, worst H2/H3,
crosstalk), `result.json` and the plots. `fit-eq` uses Rockbox's own biquad
formulas from `dsp_filter.c`: band 0 is a low shelf, bands 1-8 peaks and
band 9 a high shelf. It leaves out bands under 0.3 dB (a disabled band costs
no DSP time) and sets the preset's precut to its highest boost. `compare`
overlays the curves at 1/6 octave, normalised at 1 kHz, together with the
predicted result.

`arb audio selftest` runs the whole chain offline, on a simulated reference
and a simulated player with known faults. Run it after changing the
analysis.

## Calibrating levels

- **Volume table**: play the 1 kHz file and step the volume
  (`arb TARGET sh vol N` every 4 s), then `arb audio analyze LABEL FROM TO
  --volsteps --volumes ...`. Each step's level must match the table the
  port claims. A plateau 20-30 dB worse than its neighbours is the click of
  an unramped change, not distortion.
- **Analog gain stages**: find where the analog stage saturates against the
  digital level (the THD+N-against-level curve), and set the gain code and
  maximum volume so digital full scale stays under it. Match stock's clean
  maximum: on the YP3 that was gain code 0 with the volume capped at -8 dB,
  as stock does, which landed within 0.05 dB of stock's loaded gain.
- **DAC overload**: if 0 dBFS distorts and -1 dBFS does not, the DAC's own
  datapath saturates. Keep the maximum volume or the precut below it.
- **Clicks**: `arb audio slips FROM TO` finds whole-sample phase slips in a
  steady tone. On the YP3 they were late DMA restarts held off by the tick,
  not added noise.

## What good looks like

The release checklist ([release.md](release.md)) asks for these against the
reference and stock, measured on battery with the same load:

- response within ±0.5 dB of the reference from 50 Hz to 15 kHz, or an EQ
  preset shipped that brings it there;
- THD+N at each level no worse than stock's at the same output level;
- noise at or below the capture's floor (else state the figure);
- no clipping at maximum volume on 0 dBFS material, or the maximum capped
  below the clip point;
- no slips in 120 s of steady tone;
- crosstalk no worse than stock's under the same load.
