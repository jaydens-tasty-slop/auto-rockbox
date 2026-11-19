# Tests, soak and torture runs

Every check of a player is a **scenario**: one question with a check that
answers it, registered for every player that has what it needs. The same
scenarios run three ways:

- `arb TARGET test`: each once, in bring-up order, with a verdict. Run it
  after a change, while bringing up a driver, and on a release candidate.
- `arb TARGET soak` and `arb TARGET torture`: at random by weight for hours.
  A driver works only once it has run for hours while other things happen
  around it, and long runs find what a pass misses.

Both long runs record every step and keep going after a failure so that one
bug doesn't hide the next.

## Test passes

```
arb yp3 test build/test-elf/rockbox.elf build/test/RUN                 # everything
arb yp3 test build/test-elf/rockbox.elf build/test/RUN storage audio   # some areas
```

The soak scenarios come first (no dev shell needed), then the torture
scenarios over the dev shell. Each step passes, fails, or **skips** when it
had nothing to test (no music library on the card, no Line In capture).
`OUT/summary.md` lists every step, then the scenarios left out and what
they need: during bring-up, that list is what is left to do.

- **Areas** follow bring-up's driver order, then what builds on the
  drivers: boot, lcd, keys, storage, audio, power, usb, rtc, record, fm, bt,
  playback, ui, plugins, db (`engine.AREAS`).
- A pass uses seed 1 unless `--seed` says otherwise, so two builds make the
  same choices. `--only a,b` runs named scenarios (also those off by
  default), `--repeat N` runs N steps of each.
- One step samples one choice (one codec file, one plugin). A pass catches
  what breaks outright; torture's hours catch the rest.
- All of the YP3's torture scenarios once take about 20 minutes.

## Long runs

| | `arb TARGET soak` | `arb TARGET torture` |
|---|---|---|
| A step is | a whole boot with a key script | one action over the dev shell |
| Needs | the dev loop only | a dev shell over USB serial |
| Finds | boot, cold start, key, power and panel faults; real-key UI paths | races and leaks under load: playback with everything else, codec and plugin churn, FM, recording, USB, Bluetooth |
| Judged by | the log after each return, Line In, the contact sheet | health counters after every step, the log after each crash |

A new player gets the soak first, because it needs no shell. Torture comes
once the dev shell works. A release needs both
([release.md](release.md): 50 h of torture and 8 h of soak).

## Running

```
arb ts17 soak build/ts17/rockbox.elf build/soak-RUN --hours 8 --shots --set volume=-15
arb yp3 torture build/yp3/rockbox.elf build/torture-RUN --until 07:00 --seed 3
```

- Build the image into its **own directory** and leave it alone during the
  run. Recovery reflashes that file, so rebuilding over it changes what the
  run tests.
- **Card media comes from arb.** `arb media-tone DIR` writes the test tone
  (the `[media] tone` WAV every set is encoded from, and the MP3 the soak
  and dev runs play). Torture plays `/codectest` (one clip per codec, from
  `arb media-codectest OUT`) and the stress set
  (`arb media-stress OUT --source OUT/codectest/flac_16_44.flac`: `/stress`
  plus its playlist). Copy OUT's contents to the card root once, for
  example with `card-run 'cp -r OUT/. "$MP/"'`, plus `arb media`'s video/
  as /video and tone.mp3 into the player's dev folder. Library scenarios
  skip when the card has no music library.
- Start `arb linein serve` (and `arb webcam serve` for `--shots`) first.
  Keep Line In at unity gain, and check what is on the card before blaming
  the player (some test files step down 12 dB at 10 s by design).
- **Pin the volume** with `--set volume=DB` (every boot sets it). Random
  keys in a browse step can open the WPS, where Up and Down change the
  volume. The setting persists, so later steps play quieter until quiet
  test clips sink into Line In's noise floor.
- **Keep Bluetooth off** for wired checks. A player with Bluetooth on
  auto-connects at boot to the last device it paired with. If that device
  is this PC, the test audio goes to the PC's speakers or headphones
  instead of Line In. The TS17 soak boots with `bluetooth=off` unless
  `--set bluetooth=card` says otherwise (`Target.soak_settings`). Torture
  keeps the player's stream on a null sink when the player's `bt_address`
  is configured (its bt-* scenarios need it), and the TS17's torture boots
  with Bluetooth off when it is not.
- `--only a,b` and `--weights a=5,b=0` focus a run on one area, for
  example to check a fix. `--seed` repeats a sequence.
- Watch the run log for `FAIL`, `LOST` and tracebacks. Don't poll it.
- Don't leave a run charging unattended past the charge guard's limit.

## Reading a run

The run directory holds `steps.jsonl` (one line per step: scenario, time,
pass or fail, problems, note), `NNN-name.txt` with each step's output, and
`NNN-name.png` contact sheets (framebuffer beside webcam frames).

For each failure:

1. **Check the signal before the checker.** Look at the raw Line In
   seconds, the frames and the log lines that the checker judged. Each of
   these was once a checker bug rather than a player bug:
   - two capture seconds published within one wall second;
   - a music clip judged as a tone with dropouts;
   - the capture running about 3 s behind the player;
   - a test file that steps down by design.
2. **Find the first bad thing**, not the loudest. A hang shows up as a
   watchdog return. The thread snapshot and the tick trace (`arb yp3
   threads`, the log's last PCs) say where it spun.
3. **Reproduce it narrowly**: `--only` that scenario, then a plain `run`
   with the same keys or shell commands.
4. **Put it in the player's backlog at once** (`docs/PLAYER/TODO.md`): the run, the step, what was seen, and
   the first guess. Do this before fixing it.
5. Verify a fix with a targeted run, then let the next full run confirm it.

## Writing scenarios

A scenario is a function registered for a suite and an area. It gets a
`StepContext` (the target, the run's random generator, the step's output
path) and returns an `Outcome` (problems, a note, the output text):

```python
from auto_rockbox.engine import Outcome, StepContext, scenario
from auto_rockbox.suites.soak import boot

@scenario("sdtest", suite="soak", area="storage", weight=2)
def sdtest(step: StepContext) -> Outcome:
    out = boot(step, 90, settings={"sdtest": "512 16 odd"}).outcome
    if "errors 0" not in out.output:
        out.problems.append("SD read-back errors")
    return out
```

- **A check is a scenario, not a script.** A probe or one-off script that
  checks something (a driver just brought up, a fix) becomes a scenario in
  its area, so it runs again on the next change, in every pass and every
  long run, and on the next player.
- **Generic first.** Write it for every player: the Target API, the dev
  shell's shared commands (`apps/dev_shell.c`) or the run settings every
  dev build takes, with what differs (a path, a limit, a station) in the
  Target or the workspace config. A case found on one player is a case for
  all. Only a scenario that needs hardware or shell commands no other
  player has goes in `targets/NAME/`.
- **One question per scenario**, with a check that answers it: an expected
  log line, a tone on Line In, a frame on camera, a counter that must not
  move.
- **Randomise what the user could vary**: files, timing between keys,
  volume, the order of actions. Record the choices in the step's output
  so that a failure can be replayed.
- **No false positives.** A checker that cries wolf gets ignored. When a
  check fails on a good player, fix the check first, and add a test with
  the sample that fooled it.
- **Skip when there is nothing to test**: `return t.skip(why)` in torture,
  `Outcome(note="skipped: why", skipped=True)` in soak. A skip that passed
  silently would hide a card without its test files.
- **Health between steps** (torture): faults and panics in the log, PCM
  underruns, UI stalls over 1 s, and free memory that keeps falling
  (free plus the playback buffer while playback holds it, `abuf`: a port
  that keeps the buffer after a stop has nearly all free memory in it).
  Each counter has a budget. Going over it is a failure even when the step
  itself passed.

Every player's soak scenarios (`suites/soak.py`: boot, crash, poweroff,
sdtest, fsck, usb-hash, udelay, tone, flick, fm-station, games) use only the run settings every dev build takes
(`dev_boot`: `sdtest=`, `crash=`, `poweroff=`, `play=`, `sh=`, ...) and the
Target API, so they run on a player whose dev shell doesn't work yet. A
player's own (`targets/NAME/soak.py`) follow its menus, and one of the same
name replaces the generic one.

A touch player's list feel is a scenario, not a look at the panel: `flick`
scrolls a 60-row folder (it makes `/flick` on the card) with key-script
drags and judges the build's `fling` and `coast` log lines: every drag a
fling, taps on a coasting list caught, nothing opened, coasts at 30 fps or
more. Keep a touch test's points off the list's edges and off a mini player
(the bottom fifth while music plays): a drag that starts there isn't the
list's, and a tap there is the theme's.

The games a build must ship are the Target's `games`: each plugin with a
key script that starts a game from its menu, plays it and quits with the
player's keys. `games` runs one per step, in turn (`--only games` covers
them all in as many steps), from `sh=run` at boot, and fails a game that
never started or that the keys could not leave (no `rc 0`): a keymap bug.

A step that raises an error comes back at once, so the same error three
steps running ends the run rather than spinning until its deadline (a
stale webcam once wrote 880,000 empty steps); `--shots` checks the webcam
publisher before the first step.

### Torture suites

Every player's dev build has the same shell (`apps/dev_shell.c`), so the
torture scenarios are shared: `suites/rockbox_torture.py` holds them and
`RockboxTorture`, the suite they run in. Each player has a subclass in
`targets/NAME/torture.py`, registered with `@player_suite("NAME")`, that
declares what differs:

- The card's dev folder is the Target's `dev_dir` (the build's
  `DEV_CARD_DIR`): the settings files the steps load and the tone live
  there. By default the suite writes the settings files once per run over
  USB mass storage; the YP3 pushes them with every boot instead.
- `main_menu`, `slow_codecs`, `expected_msgs` (errors a scenario provokes
  on purpose, such as the YP3's heap limits), `charge_limit`, and
  `boot_settings()` (the TS17 boots with `bluetooth=off` unless its
  `bt_address` is configured).
- `features`, what the build has that some scenarios need (`needs=`):
  `lcd-gate`, `fm-tune`, `bt-hid` (a build with Bluetooth game controllers
  and keyboards). A player without one simply does not run those
  scenarios. What every scenario relies on comes with the shared dev layer:
  Home selects the main menu's first item, and `dryoff` turns a menu
  power-off into a return to the host. The bt-*
  scenarios need `[targets.NAME] bt_address`.
- Hooks for the player's own shell commands and records: `prepare()`,
  `gate_on()`, `fm_tune()`, `bt_fail_details()`, and the crash records
  (`records()`, `wait_host()`, `expected_return()`).

Shared torture tests that need set-up on this PC:

- `linein` and `volume` need `arb linein serve`, the tone in the dev
  folder, and the output wired to Line In at unity gain. `linein` also
  counts sample slips (clicks) in the tone's loud part; `volume` plays it
  at two volumes and checks the level moves by the step.
- `dac` plays the audio test signal at the volume of a saved analysis of
  the same player on the same bench, and holds the new analysis against
  it (gain, response, THD+N, noise, crosstalk, polarity). It is skipped
  without `[targets.NAME.dac]`: `signal` (card path), `timeline`,
  `baseline` (a `result.json` of `arb audio analyze`), `volume`.
- `hang` spins the shell's thread; the recovery watchdog must bring the
  player back by itself (`expect_return` makes that return no failure).

A torture run ends with the card's filesystem check over USB mass storage
(`--no-fsck` leaves it out); its verdict goes into report.md.

A scenario that needs commands only one player's shell has (the YP3's `fm`,
`fmregs`, `fmmon`) registers in that player's module with `target={"NAME"}`.
New shared scenarios go in `suites/rockbox_torture.py` (`rockbox_scenario`,
with its area), gated by a feature when a player may lack what they need.

## Bluetooth peers

`arb bt-peer` gives a port's own Bluetooth stack something to talk to
(Bumble: `uv sync --extra bt`):

- **`sink`** and **`hid`** run against the simulator. Bumble serves it a
  virtual controller as H4 over TCP and plays an A2DP sink with AVRCP
  controller, or a DualShock 4 or keyboard. `hid --sim DIR` starts the
  simulator itself. Build it from the branch under test: a stale build
  maps the pad keys differently. Its `simdisk/.rockbox/bluetooth.dat` keeps
  pairings between runs, so clear it when another peer paired last.
- **`pcsink`** and **`pcheadset`** use this PC's adapter against the
  player: the PC as an A2DP sink, or as a headset paging the player cycle
  after cycle. `arb yp3 bt-peer hid` is the HID device over the air.
- Each run writes its btsnoop, keys and log to its output directory. When
  a result differs from what you expect, diff two runs' HCI traffic
  packet by packet before suspecting the peer. In this port, a sink
  failure turned out to be a race inside the stack: it streams silence
  until its mixer has PCM.
- This PC's adapter can fail too. After one bt-headset step, its links
  came up and paired but carried none of the player's data (SDP and
  AVDTP unanswered; the peer's btsnoop showed nothing received). Neither
  HCI_Reset (BlueZ power off and on, Bumble's start) nor a reboot of the
  player cleared it; `usbreset VID:PID` did. Torture's Bluetooth setup
  now USB-resets the adapter once before it fails, and notes it when that
  was the fix.
