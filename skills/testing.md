# Soak and torture runs

A driver works only once it has run for hours while other things happen
around it. Two kinds of long run find what short tests miss. Both pick
scenarios at random by weight until a deadline, record every step, and keep
going after a failure so that one bug doesn't hide the next.

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
- Start `arb linein serve` (and `arb webcam serve` for `--shots`) first.
  Keep Line In at unity gain, and check what is on the card before blaming
  the player (some test files step down 12 dB at 10 s by design).
- **Pin the volume** with `--set volume=DB` (every boot sets it). Random
  keys in a browse step can open the WPS, where Up and Down change the
  volume. The setting persists, so later steps play quieter until quiet
  test clips sink into Line In's noise floor.
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
4. **Put it in `TODO.md` at once**: the run, the step, what was seen, and
   the first guess. Do this before fixing it.
5. Verify a fix with a targeted run, then let the next full run confirm it.

## Writing scenarios

A scenario is a function registered for a suite. It gets a `StepContext`
(the target, the run's random generator, the step's output path) and
returns an `Outcome` (problems, a note, the output text):

```python
from auto_rockbox.engine import Outcome, StepContext, scenario
from auto_rockbox.suites.soak import boot

@scenario("radio", suite="soak", weight=2, target={"ts17"})
def radio(step: StepContext) -> Outcome:
    return boot(step, 60, "9000 D S 1500 R R", expect=("radio: tuned",)).outcome
```

- **One question per scenario**, with a check that answers it: an expected
  log line, a tone on Line In, a frame on camera, a counter that must not
  move.
- **Randomise what the user could vary**: files, timing between keys,
  volume, the order of actions. Record the choices in the step's output
  so that a failure can be replayed.
- **No false positives.** A checker that cries wolf gets ignored. When a
  check fails on a good player, fix the check first, and add a test with
  the sample that fooled it.
- **Health between steps** (torture): faults and panics in the log, PCM
  underruns, UI stalls over 1 s, and free memory that keeps falling. Each
  counter has a budget. Going over it is a failure even when the step
  itself passed.

Scenarios for one player go in `targets/NAME/soak.py` or
`targets/NAME/torture.py`. Scenarios that only need the `Target` API go in
`suites/`.

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
