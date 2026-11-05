# FM radio

How FM came up on the YP3 and the TS17. Both have an RDA5807-family tuner
(ID 0x5808) on I2C, driven by Rockbox's own `rda5802` driver. The tuner
itself was the easy part. Nearly all the time went to what surrounds it:
**its reference clock, its antenna input, and the audio path back into
the SoC**. Each of those failed in a way that looked like something else.

## Order of work

1. **Find the tuner.** Get the I2C controller, its pins and clock gates from
   stock's FM open routine, then probe the tuner's ID register. The
   RDA5807 answers at 7-bit address 0x10 (0x20/0x21 on the wire). Pads are
   shared: on the YP3 the mic code had muxed the tuner's I2C pins, so give
   every pad one owning driver.
2. **Give it its reference clock, and prove the frequency.** See the next
   section. Do not move on until a station with RDS identifies itself at
   the frequency you tuned.
3. **Pick the antenna input.** The RDA5807 has two LNA inputs, and the
   headphone cable (the antenna) is wired to one of them. Read stock's LNA
   setting (reg05). On the YP3, LNAP gave about 20 dB more RSSI than the
   driver's default LNAN.
4. **Bring the audio back.** On both players FM is **not an analog bypass**:
   the tuner's L/R go into the SoC's ADC, get captured by DMA, and play
   through the mixer and the DAC like any PCM. That is a capture driver
   (rate, gain, DMA, IRQ priority) before you hear anything.
5. **Stereo, RDS, scanning, recording**: the Rockbox features on top.

## The reference clock: when every station is slightly off

The tuner synthesises its local oscillator from a reference clock that the
SoC provides on a pad, and register 02's CLK_MODE tells it which frequency
to expect (32.768 kHz, 12 MHz, 24 MHz, ...). If the clock and the mode
disagree, **every frequency comes out off by the same ratio**.

The YP3 ran clock source 1, which was assumed to be 32.768 kHz but is
really about 32.000 kHz, so every frequency came out **2.34% low**. "97.1"
was really 94.83 MHz, 70 kHz below Power 94.9. That error hid for two
weeks behind symptoms that each looked like something else:

- **Distorted loud bass** ("kick drums farting"): the station sat 70 kHz
  off the tuner's IF centre, so peaks of deviation hit the filter edge.
  That looks like an audio gain or clipping problem, and it was chased as
  one first.
- **FM_TRUE never asserted**, so the driver could only scan by RSSI. RSSI
  near the PC read 44-58 on empty channels against a threshold of 22, so
  every channel looked like a station.
- **Stations at odd frequencies**, which looks like a band, spacing or
  de-emphasis setting.

How to check it, in minutes, not weeks:

- **Identify a station by RDS.** Tune a strong local station, read its PI
  code (the US call letters follow from it) and look up its real
  frequency. "Tuned 97.1, decoded KENZ (PI 0x1BFB, 94.9)" gives the ratio
  at once.
- **Measure the reference** on a pad you can scope, or count it against a
  known clock (the SoC's own timers).
- **Scan with FM_TRUE**: with the right reference, the driver's own
  station test marks the real stations (20 here). With a wrong reference,
  it marks none.
- Then **listen against a phone on the same station**, back to back. The
  user's A/B found the bass distortion, and confirmed it gone after the
  fix.

Stock used source 2 (12 MHz) in the tuner's 12 MHz mode; switching to that
fixed everything at once. The TS17 needed its 24 MHz reference from the
SoC's clock-out pad: without it no tune completes at all.

The same trap waits in every derived clock. A source labelled "32 kHz" was
32.000 kHz here, and the YP3's RTC counts a 53.7 kHz internal oscillator
that the PMU measures. **Measure every clock you derive anything from**
(see [bring-up.md](bring-up.md)).

## The audio path

- **Capture rate**: stock captured FM at 16 kHz. Rockbox captures at
  44.1 kHz so the mixer needs no resampling. Either works. Know which one
  the clocks allow.
- **Gain staging**, and an ADC that **wraps instead of clipping**: the
  YP3's aux ADC, overdriven, jumps by most of full scale in one sample and
  then decays exponentially, as if the overload came before a DC-removing
  high-pass. Each overload sounds like a loud low thump, which is easy to
  blame on the tuner or the station. Record a strong station at a few gain
  codes and run `arb fm-check REC.wav` on each: it counts the wraps and
  gives the peak level, L/R correlation and side level. Pick the gain that
  leaves headroom (the YP3 uses code 22: peaks near -10 dBFS, no wraps).
  The tuner's own volume register did not change the clipping.
- **DMA IRQ priority**: the capture DMA IRQ must sit at the audio priority,
  not below the tick, or the FM monitor overruns (thousands an hour on the
  YP3 until it moved). Dev-build instrumentation in the tick costs capture
  periods too: the scheduler snapshot cost the DAC 4% of its periods.
- **Stereo**: `fm-check` on a stereo music programme reads a correlation of
  about 0.9 and side -12 to -16 dB. Correlation 1.000 means mono: the
  tuner's MONO bit, a mono programme under a stereo pilot, or one ADC
  channel feeding both sides. Try the mono bit and a second station before
  suspecting the routing. On the YP3 each 2-bit enable field needed both
  bits set (1 or 2 alone was silent), and each channel had its own gain.

## RDS

The RDA5807 decodes RDS even when stock never turns it on (reg02 bit 3
RDS_EN). Then reg 0A bit 15 (RDSR) marks a new group, 0B gives the block
errors, and 0C-0F hold blocks A-D, so one 12-byte sequential read gets the
status and a group. A poll thread costs CPU in proportion to the I2C time:
raising the I2C clock (divider 8 to 6) cut the YP3's RDS cost from 6.4% to
2.5%. Stations differ: KENZ sends no clock-time groups and puts its RT+
tags off the word boundaries, so test the decoders on the host with real
group captures, and on air with several stations.

## Tools

- **Dev shell**:
  - `fm KHZ|off` tunes and starts the monitor;
  - `fmregs` decodes the tuner's status (RSSI, stereo, STC, FM_TRUE,
    READCHAN);
  - `fmmon` reports capture statistics (overruns, gaps);
  - `fmmono`, `fmmute` and `fmgain N` switch the mono bit, mute and the
    capture gain;
  - `rds` dumps the RDS decoder;
  - `radio [KHZ]` opens the FM screen;
  - `rec SECONDS NAME fm stereo wav` records from FM.

  During bring-up a raw tuner register write is worth having, but remove it
  once the questions are answered.
- **Host**: `arb fm-check` on recordings; Line In (`arb linein`) on the
  monitor output, with the same level and dropout analysis as playback.
- **Torture** (`arb yp3 torture`): `fm` tunes, reads `fmregs` and runs the
  monitor 5-20 s, failing on new overruns. `fmui` drives the FM screen by
  keys (seek, step, leave). `record` and `recui` record from FM and the mic
  in random formats.
- **Soak** (TS17): the FM screen by keys from a cold boot.

## What good looks like

For a release ([release.md](release.md)):

- Stations decode at their labelled frequencies (RDS PI matches), and an
  FM_TRUE scan finds the real stations and only them.
- No overload wraps on the strongest local station at the shipped gain.
- Stereo on a stereo programme, and mono when forced.
- An hour of the FM monitor with no capture overruns.
- Recording from FM in every shipped format, played back clean.
- Listened to against a phone on the same station: no audible difference
  the user minds.
