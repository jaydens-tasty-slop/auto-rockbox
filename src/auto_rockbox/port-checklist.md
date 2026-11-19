# Port checklist: PLAYER

<!-- arb-checklist v1 target=PLAYER -->

The port's copy of auto-rockbox's checklist (`arb checklist init PLAYER`),
filled in as the port goes along. A merge request to the port's Rockbox
fork or to auto-rockbox carries it, and `arb checklist check FILE --through
PHASE` must pass for the phase the request claims (`skills/bring-up.md`,
"The port's checklist").

How to fill it in:

- `[x]` done: the `evidence:` line under it says when and how it is known,
  with something a reviewer can open: a commit, a run directory, a doc
  section, a fact id, a log line. "Works" is not evidence.
- `[-]` not applicable: `evidence: n/a: <why>` (the player has no tuner).
- `[ ]` open. Work in progress may say so on its evidence line.
- Keep every item and its text as the template has them; `arb checklist
  update` brings in items a newer template added. Items of this port's
  own go in any phase with an id starting `x.` (`x.touch-gestures`).

## setup: the port's own files and walls

- [ ] setup.backlog The player has its own backlog file (`docs/PLAYER/TODO.md`), not a list shared with other players; every crash and open experiment goes in it at once
  evidence:
- [ ] setup.notes The player has its own notes (`docs/PLAYER/`): hardware facts, each with how it is known (measured, fact id, or GUESS)
  evidence:
- [ ] setup.tree The port has its own branch or tree; every change to shared Rockbox code is behind the target's define, and other targets build unchanged
  evidence:
- [ ] setup.clean-room The clean-room roles and walls are set up (clean-room.md): the port session reads facts only, its settings deny the dirty side, and the commit hook refuses stock narration (n/a only with a public datasheet)
  evidence:
- [ ] setup.target An arb Target for the player, its config keys in the class docstring (adding-a-target.md)
  evidence:

## identify: what the player is (bring-up.md, Phase 0)

- [ ] identify.usb USB IDs in every mode (stock, stock's USB disk, update mode, boot ROM recovery or download mode) and how each mode is reached by hand
  evidence:
- [ ] identify.parts SoC, flash, RAM, panel, touch, codec, amplifier, tuner and radio identified, each with how it is known
  evidence:
- [ ] identify.prior-work Prior work for the SoC family found, and credited in the port's docs
  evidence:
- [ ] identify.triage The triage verdict at the top of the notes: RAM size and where code runs (XIP or not), measured through the boot ROM
  evidence:
- [ ] identify.unit The host tools tell this unit from every other player on the bench before they write to it, including players with the same USB IDs in recovery mode
  evidence:

## wayback: a way in and a way back (bring-up.md, Ground rules and Phase 1)

- [ ] wayback.rom Read memory, write RAM and run code through the boot ROM; the gadget or payload reports faults instead of wedging
  evidence:
- [ ] wayback.backup All of flash dumped twice, the two dumps identical, kept out of public repositories (path and hash)
  evidence:
- [ ] wayback.restore A scratch sector written and restored from the dump, then all of flash compared with it
  evidence:
- [ ] wayback.guard The writer refuses the boot chain stages that lead to recovery mode and the calibration and settings areas; tried and refused on hardware
  evidence:
- [ ] wayback.soft A software path into recovery mode from our code (a magic word and a reset)
  evidence:
- [ ] wayback.watchdog The recovery watchdog behind one switch (`*_RECOVERY_WATCHDOG`), armed at the earliest entry: a hang comes back to the host by itself
  evidence:
- [ ] wayback.hand A hand path into recovery mode from our own firmware (a key rule at power-on with USB in, a debug menu entry), tested on hardware from our firmware before an image boots on its own; a normal power-on still boots
  evidence:
- [ ] wayback.rescue-stub When the boot ROM checks no key: a write-once rescue stub, proven in bring-up.md's five steps and pinned by hash (n/a when the ROM has a key)
  evidence:
- [ ] wayback.usb-only Recovery mode only with USB power in: on battery a hang or crash reboots (tested with a crash armed and USB pulled)
  evidence:
- [ ] wayback.charger The charge enable bit is known and the host tools stop the charger in recovery mode (n/a when no software stops the charge, stock's included: a board charger that ends it itself)
  evidence:

## devloop: the dev loop before drivers (bring-up.md, Phase 2; adding-a-target.md)

- [ ] devloop.config A run config the image takes once, and a default return without one
  evidence:
- [ ] devloop.log A log ring that survives the return, read by `arb PLAYER log`
  evidence:
- [ ] devloop.return The timed return and `dev_return()`; an early return reads as a hang
  evidence:
- [ ] devloop.keys The key script injected at the button driver
  evidence:
- [ ] devloop.screen The framebuffer read back, and the webcam contact sheet compared with it
  evidence:
- [ ] devloop.test `arb PLAYER test boot` passes: the generic soak scenarios run on the player
  evidence:

## drivers: each works, is measured and documented, with its scenarios in `arb PLAYER test AREA` (bring-up.md, Phase 3)

- [ ] drivers.timing `udelay` measured against the tick from 1 us to 1 s
  evidence:
- [ ] drivers.constants One home for each constant: the SoC header, the board pad map with each pad's owner, the IRQ priority table, one measured clock source; files of reverse-engineered definitions carry the warning, and every entry its tag
  evidence:
- [ ] drivers.lcd LCD: logo and menus on camera match the framebuffer; backlight from off to full
  evidence:
- [ ] drivers.keys Buttons: every key, chord and hold through the key script and by hand
  evidence:
- [ ] drivers.touch Touchscreen: every edge and corner lands where it is touched, by hand and on camera; taps, holds and swipes reach the UI (n/a without one)
  evidence:
- [ ] drivers.storage Storage: reads and writes with DMA, a clean `fsck` after stress
  evidence:
- [ ] drivers.audio Audio out: the right tone and channel on every output (headphones, speaker), switching between them, no dropouts in an hour
  evidence:
- [ ] drivers.power Battery reading calibrated against a meter, charge control, real power-off, wake
  evidence:
- [ ] drivers.usb USB mass storage with hashes checked, then the dev shell over USB serial
  evidence:
- [ ] drivers.ram A RAM ledger in the docs: core, codecs and plugins fit
  evidence:
- [ ] drivers.rtc RTC and alarm survive a power-off (n/a without an RTC)
  evidence:
- [ ] drivers.fm FM: stations at their labelled frequencies, the reference clock measured (fm-radio.md) (n/a without a tuner)
  evidence:
- [ ] drivers.record Recording from the mic and line sources the player has (n/a without)
  evidence:
- [ ] drivers.bt Bluetooth (n/a without, or not shipped)
  evidence:

## product: a player someone can use (bring-up.md, Phase 4)

- [ ] product.ui Menus, file browser, settings, playback, games and a writable card work by hand
  evidence:
- [ ] product.keymap A keymap made for this player's keys: no chord the hardware can't press
  evidence:
- [ ] product.battery The battery percentage from a measured discharge table (battery.md)
  evidence:
- [ ] product.test-pass A whole `arb PLAYER test` pass: every step passes or its skip is explained, every scenario left out is a feature the port doesn't ship
  evidence:
- [ ] product.long-runs Soak and torture runs, every incident in the backlog
  evidence:

## release: release.md, results in `docs/PLAYER/release-VERSION.md`

- [ ] release.results Every release.md check recorded for the candidate: date, commit, numbers, log paths
  evidence:
- [ ] release.battery Battery runs against stock (release.md, Battery runs)
  evidence:
- [ ] release.torture At least 50 h of torture over three seeds or more, one run of 12 h or more, and 8 h of soak on the candidate
  evidence:
- [ ] release.recovery Recovery by hand from the release build, the rescue stub's rules, the way back to stock, the boot chain byte-identical to the backup
  evidence:
- [ ] release.audit The code quality audit by a reviewer who did not write the code; every finding fixed or in the backlog
  evidence:
- [ ] release.legal Built from a clean checkout of the tag, nothing derived from stock, source and licences published
  evidence:
- [ ] release.people Someone else used the candidate as their daily player for two days
  evidence:

## upstream: merge requests

- [ ] upstream.rockbox The fork's merge request carries this form; shared code changes only behind the target's define; the simulator and another target build
  evidence:
- [ ] upstream.arb The target's pull request to auto-rockbox carries its offline tests, config keys in its docstring, a README row, no personal bench values and nothing derived from stock (release.md, After release)
  evidence:
