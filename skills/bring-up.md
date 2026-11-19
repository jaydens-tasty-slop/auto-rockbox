# Bringing up Rockbox on an MP3 player

The method that took two cheap players from no documentation to a working
Rockbox: the YP3 (SL6801, dual Cortex-M4) and the TS17 (Actions Lark,
Cortex-M4F). It assumes no datasheet and no SDK. Everything comes from the
hardware, the stock firmware's behaviour and its binary.

The order matters. Each phase makes the next one cheap and safe: **a way back
before a way in, a dev loop before drivers, and measurements before
conclusions.**

## The port's checklist and backlog

Start every port with `arb checklist init PLAYER`. It copies auto-rockbox's
checklist (`src/auto_rockbox/port-checklist.md`: this page and release.md
as items, phase by phase) into the port as `docs/PLAYER/checklist.md`.
Fill it in as the port goes: a closed item carries dated evidence a
reviewer can open (a commit, a run directory, a doc section, a fact id),
and an item that doesn't apply says why. A merge request to the port's
Rockbox fork or to auto-rockbox carries the form, and
`arb checklist check PLAYER --through PHASE` must pass for the phase it
claims: `devloop` for a target whose dev loop runs, `release` for a
released port. `arb checklist update` brings in what a newer template
adds. Never reword an item to make it pass; the check compares the text.

Each player also gets its own backlog (`docs/PLAYER/TODO.md`) and notes
(`docs/PLAYER/`). A TODO shared by several players mixes their open items,
and a fix on one player reads as done for the other.

## Ground rules

- **Clean room.** Read stock firmware for facts only: register addresses,
  bit meanings, init values and tables, protocol constants, timings. Never
  transcribe its functions into the port. Write each driver from the facts,
  the way a Linux driver is written from a datasheet. Test tooling may use
  whatever works, including calling ROM routines at run time; shipped code
  may not contain anything derived from stock. Keep reverse engineering in a
  separate session or notebook that only exports facts into `docs/`; with
  agents, isolate the sessions' tools and gate what crosses
  ([clean-room.md](clean-room.md)).
- **Back up first, and prove the restore.** Before writing a byte of flash:
  dump all of it twice, compare the two dumps, and restore a non-critical
  sector from the dump to prove the path works. Keep the dumps out of any
  public repository.
- **Never touch the way back.** The boot ROM's recovery mode, and the
  bootloader stages that lead to it, are the only things that can't be fixed
  with a USB cable. Find out which flash areas they live in, and make your
  writer refuse them (the YP3 writer refuses below `0xE000` and from
  `0x1FC000` up, and the TS17 writer refuses the whole boot chain below
  `0x14000`, including stock's recovery stage fw0_rec, without `--boot`).
  Guard every stage that leads to recovery, not only the first sectors.
- **Always keep a way back from your own firmware, by hand and without a
  host.** Once the port replaces stock, stock's ways into recovery mode go
  with it: the TS17's on/off + USB rule lived in stock's application, and
  its pinhole turned out to be a full reset that wipes the boot-mode word,
  so it booted Rockbox again. Then a port image that hangs before its dev
  return (or a release image, which has none) bricks the player as far as
  the user can tell. Before the first image that boots on its own, give
  every build, release included:
  - **a key rule at power-on**, checked as the very first thing the firmware
    does (before the LCD, storage or threads): a key held with USB power in,
    still held a moment later, sets the recovery word and resets. Keep its
    total hold clear of the PMU's own long-press reset (the TS17's is ~7 s;
    its rule takes ~3.5 s). Test it on hardware, and test that a normal
    power-on press still boots;
  - **a menu entry** (System > Debug) that reboots into recovery mode
    through a clean shutdown.
  Verify every claimed way in on hardware, from the port's own firmware,
  not from stock: what reached recovery with stock flashed may not with the
  port flashed.
- **No key into recovery in the boot ROM? Put a write-once rescue stub ahead
  of the firmware.** The YP3's ROM checks Down at reset, so nothing flashed
  can take that away. The TS17's ROM checks no key or pin, so its key rule
  lived in the very image the dev loop rewrites daily. A wrong file (an ELF
  instead of the flat image), a half-written image or a startup regression
  then strands the player before the rule runs. The fix is a small stub in
  the image's first erase sector, where the boot chain jumps, with the
  firmware linked right after it (TS17: `rescue-lark.S`, ~70 instructions).
  The stub:
  - sets the recovery word and starts the watchdog first, and never feeds
    it, so any hang or fault after those stores lands in recovery;
  - checks the key rule, with USB power required;
  - checks the firmware's vectors (SP in RAM, reset vector inside the image)
    and goes to recovery if they are wrong, but only with USB power in. On
    battery it powers off instead;
  - jumps with the recovery word still set, uses no stack, and leaves the
    caller's registers alone;
  - depends only on its own constants, so every build carries identical
    bytes, which can be pinned by hash.

  Prefer a key that is easy to reach with cables in. The TS17 accepts the
  pad's Down as well as on/off, and since plugging USB in powers it on,
  Down held while plugging in is enough.

  **Prove it before it runs from a cold boot**, in this order:
  1. Emulate the stub's bytes from a build on every branch, with modelled
     registers and no RAM mapped (`tests/ts17/test_rescue_stub.py`,
     unicorn). Check that broken copies fail the test.
  2. Write it, but don't reset. Writing runs nothing, and recovery mode
     keeps the player until you launch. Until the launches pass, ask the
     user not to power-cycle it.
  3. Launch it from recovery mode, once per branch. Its safety net must be
     real: confirm which component keeps the recovery word set and which
     watchdog fires if the launch dies (TS17: the hook re-arms the word on
     every call, and the ROM's watchdog fires ~11 s after its USB loop
     stops). Don't claim the tools set it without reading the launch code.
     For a key, read it from the host and launch the moment it is held. For
     bad vectors, blank the firmware's first sector with the writer, then
     put it back. A recovery branch returns in about 1-2 s; a wedge takes
     the watchdog's ~11 s.
  4. Cold boot through the boot chain with no key, then the hand gestures
     (key held while plugging in; the PMU's long-press reset out of recovery
     mode on battery).
  5. The battery branch. A dark screen can't tell "off" from "recovery
     mode", so use a clock that a reset keeps and a power-off stops (TS17:
     RTC_0's run bit).

  **Then pin it, and freeze it once it ships.** A bad stub on a player that
  has no other way back is a brick, and a stub that is never rewritten
  can't go bad. Until the first release a revision is fine (new bytes,
  re-pinned everywhere at once, the five steps above again on every
  player); after it, never:
  - Pin its bytes by hash in the dev writer and the user flasher, and fail
    the build if its source assembles to anything else (TS17: `nor.py`
    `RESCUE_SHA256`, `ts17dev/build.sh`).
  - Keep it out of release packages: they hold the firmware alone, after
    it. Bump the package format so older flashers refuse them; otherwise
    an old flasher writes the firmware where the stub belongs and leaves
    stock's start jumping into it.
  - The user flasher carries its own copy and writes it only where there is
    none, stub first and verified, then the firmware. A cable pulled after
    the stub then comes back in recovery mode, and the only window that
    can't be recovered is the stub's own sector, once per player. Any stub
    already there stays, even one with another hash.
  - No override and no question. A user can't judge "replace the rescue
    stub?" and will say yes. Changing it would take a new flasher release
    with its own migration logic, decided by the owner.
  - The dev writer follows the same rules: `--rescue` only installs the
    pinned stub where there is none, or removes it to go back to stock. It
    refuses the firmware alone unless a stub is stored ahead of it.
- **Recovery mode only with USB power in.** A boot ROM's download mode
  often has no timeout, and the screen stays dark (the TS17 held it for 9 h
  and stays in it when unplugged). On battery it looks off while it drains
  the cell, which has no cutoff below the firmware's own. So keep the
  recovery word set only while USB power is in: on battery a hang or crash
  reboots the firmware instead, which the user can see, and the firmware's
  low-battery power-off stays in charge. A dev build's timed return waits
  for USB power too. Test it with a crash armed and USB pulled before it
  fires: the firmware must come back by itself.
- **Know your charger.** Boot ROM USB modes often leave the charger on with
  nobody supervising it. A player left in that mode for hours goes far past
  a normal full charge. Find the charge enable bit early, and make the host
  tools switch it off. `arb` guards it on every run.
- **One process per device.** The tools lock each device. Never run two
  tools against the same player.
- **Tell units apart before writing.** Players on one SoC share the boot
  ROM's USB IDs: the TS17 and the TS28 are both `10d6:10d6` in download
  mode, so a tool that finds "the" device may have the other player. A
  tool that writes flash checks which unit it has (a chip unique ID, or
  the bench's USB port pinned in the config) before its first write.
  Until it can, only one such player sits in recovery mode on USB at a
  time.
- **Before every hardware step, write down what it tests and why.** A step
  without a question wastes a reflash and teaches nothing.
- **When the user's hands are the test, watch with something that tells you
  at once.** A background log that only reports when it exits isn't
  watching. The user then retries, wondering why nothing happened. Run a
  command that exits on the expected event (a key read from the host, the
  device leaving or joining the bus), and arm it before giving the steps.
  Give the steps in the order the player's state allows: you can't start
  from "off" while it sits in recovery mode.

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

**A second player on a SoC you already support** starts further along:
the boot ROM link, the flash tools and the chip's drivers carry over (the
TS28 after the TS17). Two things don't:

- **Board facts.** The panel, keys, pads, amplifier, tuner, flash layout
  and every pin are this board's. Ask for them from this player's own
  stock firmware or measure them; a sibling's board fact is a guess here.
- **The way back.** The hand path into recovery mode may live in stock's
  application (the TS17's did). Keep stock bootable until this player's
  own way back is proven: dev images go into a free flash range beside
  stock if there is one, and nothing is written over stock's application
  before you know what the boot chain does without it.

### Triage: can Rockbox fit at all?

Answer this before Phase 1. It takes an hour, and the port takes weeks.
Players at the same price differ by more than ten times in RAM. The
US212D (Actions ATJ2127) had 168 KiB of SRAM and the TS17 4 MiB of PSRAM,
so the price tells you nothing.

Two numbers decide it: **how much RAM**, and **where code can run**.

- **Code from flash (XIP)** needs NOR behind a cache, or PSRAM. NAND, or
  an uncached core, means all code and data live in RAM at once. A core
  with no MMU can't page code in from storage either. Stock firmware on
  such chips swaps overlays in from NAND, which Rockbox doesn't do.
- **The floor, from the ports:** the YP3 runs a 640-770 KB core from cached
  XIP NOR with 512 KiB of SRAM. Its static data alone is 153 KB, plus a
  127 KB codec/plugin arena, and it is tight. Without XIP, everything it
  keeps in flash moves into RAM: plan on 1 MiB or more (GUESS from those
  numbers). Upstream's smallest targets have 2 MiB (`memory=2` in
  `tools/configure`).
- **Verdict examples:** TS17, 4 MiB PSRAM: easy. YP3, 512 KiB SRAM + XIP
  NOR: tight but done. US212D, 168 KiB SRAM, NAND only, M4K core with no
  cache or MMU: dropped after one evening.

Measure it through the boot ROM, with no case opening, read-only first:

1. **Read the RAM window** with the ROM's read command. Uninitialised SRAM
   reads as noise (close to 8 bits/byte of entropy). The ROM area often
   reads as zeros. The ROM's own data (USB descriptors, its stack, code it
   copied into RAM) has structure; leave it alone.
2. **Write, read back and restore single words** in the noisy regions, up to
   the top. The edges give the size. Edges that refuse writes may be
   guarded by the ROM, not missing RAM: note them as unknown.
3. **CPU**: read the ID and cache configuration (CPUID, or PRId and
   Config1). Time the cycle counter (DWT CYCCNT, CP0 Count) against the
   host's clock across two gadget runs a few seconds apart, and time a fixed
   loop for cycles per instruction. Note the PLL setting you measured at.
   The top clock comes from the vendor's clock levels.
4. **Storage**: read the NAND ID or SPI NOR JEDEC ID. NAND with no DRAM or
   PSRAM is the red flag.

Vendor SDKs leaked on GitHub often carry linker scripts with the memory
map. Use them to aim the measurements, read them through a subagent that
returns facts only ([clean-room.md](clean-room.md)), and confirm on
hardware.

Send a boot ROM only the commands you know it implements. The ATJ2127 ROM
answered an unknown one (execute) with a failure status, then hung on the
next command, EP0 included. Only a power cycle by hand brought it back.
Write the verdict and its numbers at the top of the player's notes.

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
7. **A hand path into recovery mode from your own firmware** (Ground rules:
   a key rule at power-on, a menu entry), in place before an image boots on
   its own from flash. Note which resets keep the recovery word (watchdog
   and core resets usually do; a pinhole or PMU reset may not).

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
  without host config. Off the cable that default reads as a crash every
  N seconds (the TS17's 90 s did, mid-test, in front of the user): launch
  "never return" before handing a dev build to someone;
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
| 2 | Buttons | every key, chord and hold through the key script and by hand. The key script injects past the hardware, so only a hand press proves a chord: keys sharing an ADC ladder read one at a time, and the keymap must not chord them |
| 3 | Storage | card read/write with DMA, FAT checked by `fsck` after stress |
| 4 | Audio out | tone on Line In at the right frequency and channel, no dropouts in an hour |
| 5 | Power | battery reading calibrated against a meter, charge control, real power-off, wake |
| 6 | USB | mass storage with hashes checked, then the serial dev shell |
| 7 | RAM layout | core, codecs and plugins fit; a RAM ledger in `docs/` |
| 8 | RTC, alarm | survives power-off; an alarm wakes the player |
| 9 | Recording, FM, mic | as the hardware has them; FM: [fm-radio.md](fm-radio.md) |
| 10 | Bluetooth | last: it is the largest by far |

For each driver: collect the facts (registers, init tables, timings) into
`docs/hardware.md`, write the smallest test that answers one question, and
read the result off the log, Line In or the camera, never off a guess. Write
that test as a scenario in the driver's area, for every player where it can
be, not as a one-off script: `arb TARGET test AREA` then answers it again
after every change. Then soak it: [testing.md](testing.md).

### One home for every constant, and guesses marked as guesses

Magic numbers and definitions scattered across drivers caused several of the
YP3's bugs:

- The mic driver brought its own pad setup, copied from stock's generic mic
  path, which muxed four pads to analog. On this board those pads were the
  panel reset and the tuner's I2C, so starting the mic held the panel in
  reset (a white screen until reboot).
- The LCD, SD and backlight drivers each had a private copy of the pad
  routine, and other drivers wrote pad registers directly. Nothing knew
  which driver owned a pad.
- One pad configuration word holds fields for several pads. A driver's
  read-modify-write raced the key scan in the tick until all pad writes
  went through one routine with IRQs off.
- The FM capture DMA IRQ sat below the tick's priority, and the FM monitor
  overran thousands of times an hour.
- A tuner clock source written down as "32 kHz" read as a fact. It was a
  guess, the clock was 32.000 kHz rather than 32.768, and every station came
  out 2.34% low ([fm-radio.md](fm-radio.md)).
- Shared registers (core select, GPIO base, USB IRQs) were defined again in
  several drivers, and the copies could drift apart.

The rules that came out of it:

- **Name every number.** No bare addresses, bit masks, pad numbers, IRQ
  numbers or priorities in driver code. A value used once still gets a name
  and a comment saying what it is.
- **One home for each kind of fact:**
  - Registers that several drivers share go in the SoC header
    (`firmware/export/<soc>.h`). Registers only one driver uses go at the
    top of that driver.
  - The **board pad map** gets one header that lists every pad the board
    uses, with its function and its owning driver. One pad routine
    configures pads and refuses a pad that another driver owns (logged in
    dev builds). No driver writes pad registers itself.
  - **IRQ numbers and priorities** go in one table, with the order stated
    and explained (audio DMA above the tick, and so on).
  - **Clocks**: derive every rate from one named, measured source rate.
- **Write down your best guess for everything, and mark it as a guess.**
  Without a datasheet, most names, pin roles, bit meanings and values start
  as guesses. Write the guess anyway: a named guess
  (`TUNER_REFCLK_32K`) can be found, checked and fixed, and a bare `1`
  cannot. But a guess written down looks exactly like a fact, so:
  - Put a warning at the top of every file of such definitions, saying
    they do not come from a datasheet, that entries marked as guesses may
    be wrong and misleading, and where the evidence is kept.
  - Tag each entry with how it is known: measured, read from stock (with
    the routine or address), or `GUESS` (with what it rests on).
  - Give a register or bit whose role is unknown an honest name (by its
    address, "role unknown"), not an invented one.
  - When a guess is confirmed or disproved, change its tag in the same
    commit. When a driver misbehaves, grep `GUESS` in everything it touches
    before anything else.

  For example:

  ```c
  /*
   * Board pads. NOT FROM A DATASHEET: names and roles come from reverse
   * engineering the stock firmware and from measurements on the board.
   * Entries tagged GUESS are unverified and may be wrong or misleading.
   * Evidence for each entry: docs/hardware.md, "Pads".
   */
  #define PAD_LCD_RESET    PAD(1, 10)  /* measured: low holds the panel blank */
  #define PAD_TUNER_SDA    PAD(1, 13)  /* stock FM open routine */
  #define PAD_AMP_ENABLE   PAD(0, 6)   /* GUESS: stock sets it before playback */
  ```

- Keep the evidence in `docs/hardware.md` and the tag in the code, so a
  reader of the code sees how far to trust each line.

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
- **Measure every clock you derive anything from.** Labels lie: the YP3's
  "32 kHz" tuner reference was 32.000 kHz, not 32.768, so every FM station
  came out 2.34% low, and that showed up as distorted bass and a broken
  station scan, not as a wrong frequency. Its RTC counts a 53.7 kHz
  oscillator, and the TS17's `udelay` overflowed. Check rates against a
  known clock (a timer, the host, an RDS station) before tuning anything
  that depends on them.
- **Don't trust a checker over the signal.** When a test fails, look at the
  raw samples or frames before blaming the firmware. Capture stamps can
  collide (two seconds published in one wall second), a test file can be
  music rather than tone, and the capture can run seconds behind the player.
- **A power-off needs a proven wake path** before you test it unattended:
  prove the wake while the player is on.
- **Docs are facts, not diaries.** Keep the player's notes, driver status
  and backlog (`docs/PLAYER/TODO.md`) current and short, and put every
  crash and open experiment in the backlog at once. Leave out session
  transcripts.
- **Temporary hacks get `TODO(cleanup)`.** No diagnostic scaffolding in
  normal builds. One-off probe commands come out of the dev shell once their
  question is answered.
