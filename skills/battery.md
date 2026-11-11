# Battery runs

How long a player lasts on stock and on Rockbox, and the discharge curve
that Rockbox's battery percentage needs. A run takes 15-25 hours, so a sloppy
setup wastes a day: fix the conditions first, then run.

## What to run

| Run | Firmware | Output |
|---|---|---|
| S-A | stock | wired: headphone jack into the PC's Line In |
| S-B | stock (if it has Bluetooth) | A2DP to the PC |
| R-A | Rockbox release + battery logger | wired |
| R-B | Rockbox release + battery logger (if the port ships BT) | A2DP to the PC |

Do one run each, in the order S-A, R-A, S-B, R-B. Run a pair a second time
only if its results disagree with expectations: two runs of the same setup
differ by a few percent anyway. For a release, use the matrix as written in
[release.md](release.md).

## The person at the player

Some of the setup is physical, and the agent cannot do it or see it (the
webcam may not show the player). Before the first run, ask the user to set
up, and to confirm, each of these. Do not start a run on an assumption:

- **Splitter and headphones**: the player's jack into a splitter, with the
  headphones on one side and the cable to the PC's Line In on the other. A
  dev rig often has the jack straight into Line In (no load), so check this
  before every A run, not only the first.
- **USB out**: unplugged at the start of the run and left out until it
  ends. Tools can stop the charger, but only a person can take VBUS away.
- **Power off, rest, power on**: after the charge, at the player.
- **Stock's own settings and Play**: stock has no dev shell, so its volume
  step, repeat mode, backlight, Bluetooth and the start are pressed by hand.
  Give the user the exact steps, and read the level on Line In while they
  try volume steps.
- **The room and the PC's place**: temperature, no sun or heater; in
  Bluetooth runs the PC 1 m away.

## Fixed conditions

Write these down in the port's results file before the first run, and keep
them identical for every run:

- **Music**: one folder of CBR MP3s (128 kbit/s, 44.1 kHz, no album art, about
  2 hours), copied to every card as `/battery/`, on repeat all with shuffle
  off. Use real music, not a tone: a decoder's load depends on the signal.
- **Level**: play the reference tone (`arb media`'s `tone.mp3`) and set each
  firmware's volume step so Line In reads the same RMS within 0.5 dB
  (`arb linein analyse`). Note the step numbers. Line In stays at unity gain
  for the whole session.
- **Load**: the same real headphones (about 32 ohm) on a splitter together
  with Line In. A line input alone barely loads the amplifier.
- **Display**: the shortest backlight timeout, the same brightness, and the
  playing screen left on. EQ, crossfade and ReplayGain off.
- **Radio**: Bluetooth off in wired runs. In Bluetooth runs, the PC sits at a
  fixed distance (1 m).
- **Charge**: charge to the charger's own full event, unplug, power off, and
  rest 30 minutes before starting. Never start above the full point.
  Flashing tools that leave the charger on in the boot ROM's USB mode
  overcharge the cell, which makes that run look long.
- **Room**: note the temperature. No sun, no heater.
- **USB**: unplugged for the whole run (VBUS would power and charge it).
- **PC**: hold off suspend for the whole run
  (`systemd-inhibit --what=sleep:idle --mode=block sleep 108000`).

## Start, watch, stop

Wired:

```
arb linein serve --level-log build/battery/linein-RA1.csv   # leave running
# press play on the player; t0 = the first loud second in the log
arb bench end build/battery/linein-RA1.csv 2026-10-27T03:58:56Z   # exit 0 once over
```

Bluetooth: route the player's stream into a null sink, so nobody hears it
and the level log has something to read.

```
pactl load-module module-null-sink sink_name=battnull
arb bench bt-watch AA:BB:CC:DD:EE:FF build/battery/bt-conn-RB1.csv --sink battnull
arb bench level-log battnull.monitor build/battery/bt-level-RB1.csv
```

`bt-watch` exits 0 when the player stops answering (the run is over) and 2
when it drops and comes back (a range problem: check the run). Stock players
often cannot reconnect by themselves, so it pages them for 5 minutes after a
drop.

The run ends at the player's own power-off. Hours = the last loud second
minus t0, from the host's logs. A log gap (the PC suspended, a logger
restart) is fine if the player was playing on both sides of it. Note it in
the results.

## Rockbox's own log

A plugin cannot log a battery run: a resident plugin holds the memory the
codec needs. Build the release configuration with the port's battery logger
switched on instead (`YP3_BATTLOG` on the YP3: `apps/yp3_battlog.c`). It is a
small core thread that runs only if `/battlog.csv` exists on the card (an
empty file arms it). Once a minute it appends `uptime_s,mv,level,playing,
charging,cpu_mhz` with open/append/close, so a cut loses at most a minute.
It also writes a line at power-off with the reason, and a header per boot.
Without the switch, the logger build equals the release build.

A port without that logger: add one first, in the same shape. It is the only
way to tell a clean power-off at the cutoff from a brown-out.

## Turning a run into tables

- `arb bench curve build/battery/battlog-RA1.csv --from-mv 4075` builds the 11
  entries of `percent_to_volt_discharge`: for each 10% of the time remaining,
  it takes the median millivolts over ±2 minutes. Start the clock where
  playback first reads the voltage of a normal full charge, so a run started
  above full still gives a normal table.
- The charge table (`percent_to_volt_charge`) comes from a charge log: the
  charging reading at each 10% of the charge time. On the YP3,
  `arb yp3 charge-log` charges from the ROM USB state as stock does. It
  pauses every 10 minutes for a near-rest reading.
- Readings at rest sit above readings under playback load (on the YP3, about
  72 mV). If the port shows a percentage while plugged in, it needs a rest
  table too.
- Check `battery_level_disksafe` and `battery_level_shutoff` against the last
  minutes of the log. Match stock's cutoff unless you have a reason not to.

## Short measurements

To compare the current of different features in minutes rather than a day,
use a USB power meter on VBUS. Run a dev build with charging switched off,
and take 60-second means per state (menu idle, wired playback, Bluetooth
playback, CPU boosted). First check that the reading changes with load, which
shows the system really runs from VBUS. These numbers rank features against
each other. They do not replace the full runs.
