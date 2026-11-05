# Releasing a port

A release is a build someone will flash onto a player they care about and use
every day without a PC nearby. So the checks below are about that person: it
must not lose their music or their settings, it must not damage the battery,
it must last about as long as stock does, and they must always be able to go
back to stock.

Run every check on the **release candidate**: the exact commit and release
build configuration you will publish. A fix makes a new candidate. Results
from the old candidate carry over only where the diff between the two is
reviewed and clearly cannot affect them. Record each result (date, candidate
commit, numbers, log paths) in the port's `docs/release-<version>.md`. A box
without a recorded result is not ticked.

## Hard requirements

Nothing ships until every item here passes or the person who owns the release
explicitly waives it in writing in the release notes.

### Battery runs

Full runs from a normal full charge (charger stopped by its own full event,
then 30 min rest) to the player's own power-off, playing the fixed music set
on repeat. Method, helpers and conditions: [battery.md](battery.md).

- [ ] **Stock, wired**: the baseline. Run it once per battery; reuse it
      across releases while the battery is the same.
- [ ] **Stock, Bluetooth**, when stock has Bluetooth: the baseline for the
      Bluetooth run.
- [ ] **Rockbox, wired**, release build plus only the battery logger.
- [ ] **Rockbox, Bluetooth**, when the port ships Bluetooth: A2DP to a real
      sink at a fixed distance.

Each Rockbox run must show all of these:

- [ ] Continuous playback: no gaps on the Line In level log (wired) and no
      link drops in the Bluetooth watch log.
- [ ] A clean power-off at the configured cutoff, logged by the battery
      logger. A brown-out (the log just stops above the cutoff) is a failure.
- [ ] No card writes below `battery_level_disksafe`.
- [ ] A clean card check (`fsck`) afterwards.
- [ ] Hours within 10% of the matching stock run, or the gap explained (for
      example, Bluetooth on a radio that stock drives differently) and stated
      in the release notes. A regression of more than 10% against the
      previous release blocks.
- [ ] The discharge table (`arb bench curve`) checked against the run. The
      battery percentage must track the time left within 10 points over the
      whole run.

### Torture

- [ ] **At least 50 hours** of `arb TARGET torture` on the release candidate.
      Use at least three seeds and include one continuous run of 12 h or
      more. Up to half the hours may come from the previous candidate if the
      diff passes the carry-over review above.
- [ ] Every scenario the port supports ran. That includes Bluetooth, FM,
      recording and USB if they ship, and the menu crawl's coverage reached
      every menu.
- [ ] Zero unexplained faults, panics, hangs or watchdog returns. Every
      incident has a `TODO.md` entry and is either fixed (with the fix
      verified by a targeted run) or listed as a known issue with its
      trigger.
- [ ] Health counters within budget: no playback underruns outside the
      scenarios that are meant to provoke them, no UI stall over 1 s, and
      no free-memory drift across the run.
- [ ] At least 8 hours of `arb TARGET soak` (the cold boot and real key
      scenarios) on the same candidate, with every step passing or explained.

### Recovery and install

- [ ] **The way back to stock works from the release build**: follow the
      published recovery steps literally on a player running the candidate.
      Stock must boot, play and keep its settings or calibration.
- [ ] The boot chain, partition table and calibration areas are byte-identical
      to the player's own backup after install, a day of use and the
      recovery. Compare flash reads outside the firmware area.
- [ ] A fresh install over stock, following the user guide literally on a
      card that has never seen Rockbox.
- [ ] An upgrade over the previous release keeps settings, playlists and the
      database. A settings reset works.
- [ ] A hang or crash in the release build ends in a reboot or power-off,
      not a dead player that needs the battery to run flat (recovery
      watchdog policy for release builds, tested with `hang`/`crash` from
      the dev build of the same commit).

### Power and charging

- [ ] Charging to full stops on its own, both while running and while off.
      The cell's rest voltage after the full event is no higher than stock's.
- [ ] The charge limit (if the port has one) holds under load and at idle.
- [ ] Power-off from the menu and by the power key; the player stays off
      (its drain while off is measured, or at least 24 h off costs under 2%).
      It wakes by the power key, and by the alarm if it ships.
- [ ] Idle power-off and sleep timer fire.
- [ ] Plugging and unplugging USB during playback, while off and at the
      cutoff.

### Storage

- [ ] USB mass storage: copy at least 1 GB of mixed files onto the card and
      back, with every hash equal, then a clean `fsck`.
- [ ] A clean card check after the torture and battery runs.
- [ ] A full card, an empty card, a large card (the biggest the player
      takes), and a card with deep folders and long Unicode names.

### Audio

- [ ] Every shipped codec plays its clip from the media set
      (`arb media`; the manifest says which clips are tones): tone frequency
      and channel checked on Line In.
- [ ] One hour of continuous wired playback with no dropouts on Line In.
- [ ] The level at each volume step matches the table, and there is no
      clipping at maximum on the reference tone.
- [ ] Headphone detect, if the hardware has it.
- [ ] Bluetooth (if shipped): pair from scratch, reconnect after a power
      cycle of either side, two different sink models, a range drop and
      recovery, AVRCP keys.

### Interface

- [ ] Every shipped plugin starts and exits cleanly (the torture plugin
      sweep), and every game is playable by hand for a minute.
- [ ] Webcam shots match the framebuffer in every scenario's contact sheet:
      no panel faults, flashes or washed-out frames at boot or wake.
- [ ] Every shipped theme and font renders on the device.
- [ ] RTC and alarm (if shipped) survive a power-off.

### Build and legal

- [ ] Built from a clean checkout of the tagged commit with the release
      configuration: no dev flags (`*_DEV`), no dev shell, no
      `TODO(cleanup)` diagnostics compiled in. No new compiler warnings in
      target code.
- [ ] The version string on the device matches the tag. The release zip has
      a published checksum.
- [ ] The exact source is public (GPL). Licence texts and third-party
      credits are on the device (Legal Notices) and in the zip.
- [ ] Nothing derived from stock firmware is in the release: no stock code,
      binaries or dumps. Register facts and data tables are fine where the
      port's rules say so.
- [ ] User guide (install, buttons, recovery, charging), changelog, known
      issues, and the battery results table, all for this version.

### People

- [ ] Someone other than the developer used the candidate as their daily
      player for at least two days: real headphones, their own music, and
      Bluetooth if it ships. Every complaint is triaged in `TODO.md`.

## After release

- Keep the release candidate's logs and the results file. The next release's
  "regression against the previous release" checks need them.
- Track field reports in `TODO.md` with the release version in the entry.
