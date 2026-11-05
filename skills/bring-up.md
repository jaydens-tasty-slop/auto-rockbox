# Bringing up Rockbox on an MP3 player

The method that took two cheap players from no documentation to a working
Rockbox: the YP3 (SL6801, dual Cortex-M4) and the TS17 (Actions Lark,
Cortex-M4F). It assumes no datasheet and no SDK. Everything comes from the
hardware, the stock firmware's behaviour and its binary.

The order matters. Each phase makes the next one cheap and safe: **a way back
before a way in, a dev loop before drivers, and measurements before
conclusions.**

## Ground rules

- **Clean room.** Read stock firmware for facts only: register addresses,
  bit meanings, init values and tables, protocol constants, timings. Never
  transcribe its functions into the port. Write each driver from the facts,
  the way a Linux driver is written from a datasheet. Test tooling may use
  whatever works, including calling ROM routines at run time; shipped code
  may not contain anything derived from stock. Keep reverse engineering in a
  separate session or notebook that only exports facts into `docs/`.
- **Back up first, and prove the restore.** Before writing a byte of flash:
  dump all of it twice, compare the two dumps, and restore a non-critical
  sector from the dump to prove the path works. Keep the dumps out of any
  public repository.
- **Never touch the way back.** The boot ROM's recovery mode, and the
  bootloader stages that lead to it, are the only things that can't be fixed
  with a USB cable. Find out which flash areas they live in, and make your
  writer refuse them (the YP3 writer refuses below `0xE000` and from
  `0x1FC000` up, and the TS17 writer refuses the boot sectors without
  `--boot`).
- **Know your charger.** Boot ROM USB modes often leave the charger on with
  nobody supervising it. A player left in that mode for hours goes far past
  a normal full charge. Find the charge enable bit early, and make the host
  tools switch it off. `arb` guards it on every run.
- **One process per device.** The tools lock each device. Never run two
  tools against the same player.
- **Before every hardware step, write down what it tests and why.** A step
  without a question wastes a reflash and teaches nothing.

## Phase 0: identify

- USB IDs in every mode: stock running, stock's own USB disk, any update
  mode, the boot ROM's recovery or download mode (and how to reach it: a key
  held at reset, a test point, a vendor SCSI command from stock).
- The SoC, flash, RAM, panel, codec and radio, from a teardown (photographs
  of the chip markings) and from strings in the stock update image.
- Prior work for the SoC family: flashing tools, ROM protocol write-ups,
  decryption tools. Credit them in the port's docs.
- The stock firmware's layout: partition table, the stages of the boot chain,
  what each stage checks before running the next (signatures, CRCs, nothing).

Record facts in `docs/hardware.md` as you find them, each with how you know
it (measured, read from stock at an address, or guessed).

## Phase 1: a way in and a way back

1. **Talk to the boot ROM**: read memory, write RAM, run code. Usually there
   is a documented-by-someone protocol (SL6801's ROM USB, Actions' ADFU) or a
   vendor tool to watch on the wire.
2. **Run your own code in RAM** with a small payload or gadget. Give it a
   fault handler, so a bad register poke reports instead of wedging the
   chip.
3. **Dump and verify the flash.** If reads come back scrambled, model the
   scrambling (the TS17's NOR read randomizer) and check the model on
   hardware before trusting a write.
4. **Erase and program**: restore a scratch sector, then compare all of flash
   against the dump.
5. **A software path into recovery mode** from stock (vendor command) and from
   your own code (a magic word plus a reset). Then the dev loop never needs a
   hand on the player.
6. **Self-recovery from wedges**: arm the watchdog with the "go to recovery"
   reason before running anything risky, so a hang comes back to the host by
   itself. Make it one compile switch (`*_RECOVERY_WATCHDOG`), on during
   bring-up.

## Phase 2: the dev loop

Before any driver, build the loop that every later step uses: flash or load
an image, run it for N seconds, come back to the host by itself, and leave a
log and a framebuffer behind. See [adding-a-target.md](adding-a-target.md) for
the contract and [dev-loop.md](dev-loop.md) for using it. Its parts:

- a **run config** the host writes where the image finds it (return time, key
  script, settings), consumed once;
- a **log ring** in RAM that survives the return (watchdog resets keep RAM;
  some PMU resets do not);
- a **timed return** to recovery mode, plus a default for images that boot
  without host config;
- a **key script** injected at the button driver, so menus can be driven
  without hands;
- the **framebuffer** read back after the return, and a **webcam** on the
  panel. The panel can show faults (timing, init order, a washed-out picture)
  that never appear in the framebuffer, so compare the two in every contact
  sheet.

Then add a **dev shell** over Rockbox's own USB serial as soon as USB works.
It turns a 60-second reflash cycle into a one-second command, and it carries
the torture tests.

## Phase 3: boot and drivers

Boot path first: how the boot chain starts the firmware stage, the image
format it expects, and XIP or load-to-RAM. Then startup: clocks as the ROM
leaves them, a tick timer, `udelay`. **Measure `udelay` against the tick for
1 µs to 1 s**: an overflow there (the TS17's `us * ticks_per_ms` passed 32
bits at 44 ms) shows up much later as a panel that looks washed out after
init.

Driver order, each to "works, measured, documented" before the next:

| Order | Driver | Done when |
|---|---|---|
| 1 | LCD | logo and menus on camera match the framebuffer, backlight PWM |
| 2 | Buttons | every key, chords and holds through the key script and by hand |
| 3 | Storage | card read/write with DMA, FAT checked by `fsck` after stress |
| 4 | Audio out | tone on Line In at the right frequency and channel, no dropouts in an hour |
| 5 | Power | battery reading calibrated against a meter, charge control, real power-off, wake |
| 6 | USB | mass storage with hashes checked, then the serial dev shell |
| 7 | RAM layout | core, codecs and plugins fit; a RAM ledger in `docs/` |
| 8 | RTC, alarm | survives power-off; an alarm wakes the player |
| 9 | Recording, FM, mic | as the hardware has them |
| 10 | Bluetooth | last: it is the largest by far |

For each driver: collect the facts (registers, init tables, timings) into
`docs/hardware.md`, write the smallest test that answers one question, and
read the result off the log, Line In or the camera, never off a guess. Then
soak it: [testing.md](testing.md).

## Phase 4: make it a product

Normal menus, file browser, settings, playback, a few games, a writable card,
battery percentage from a measured curve, charge control, real power-off.
Then the torture suite until it runs clean for days, the battery runs
([battery.md](battery.md)), and the release checklist
([release.md](release.md)).

## Habits that paid off

- **Keep the shared tree clean.** Every port-only change to shared Rockbox
  code goes behind the target's own define, so no other target changes.
  Simulator builds undefine `CONFIG_CPU`, so gate UI and policy code on the
  target define instead.
- **Rockbox threads are not preempted.** Anything that holds the CPU longer
  than the PCM buffer (around 185 ms on the YP3) causes an underrun. Yield by
  elapsed time, not by work count.
- **Small RAM means a ledger.** Write down every static buffer and stack with
  its size, and decide RAM against features against flash in that order.
- **Don't trust a checker over the signal.** When a test fails, look at the
  raw samples or frames before blaming the firmware. Capture stamps can
  collide (two seconds published in one wall second), a test file can be
  music rather than tone, and the capture can run seconds behind the player.
- **A power-off needs a proven wake path** before you test it unattended:
  prove the wake while the player is on.
- **Docs are facts, not diaries.** Keep `docs/hardware.md`,
  `docs/driver-status.md` and `TODO.md` current and short, and put every
  crash and open experiment in `TODO.md` at once. Leave out session
  transcripts.
- **Temporary hacks get `TODO(cleanup)`.** No diagnostic scaffolding in
  normal builds. One-off probe commands come out of the dev shell once their
  question is answered.
