# The dev loop with `arb`

Everyday use: build, run on the player, read what came back. The contract a
dev build keeps on the device is in [adding-a-target.md](adding-a-target.md).

## Setup

- `auto-rockbox` sits in (or next to) the port's checkout. The port's
  `auto-rockbox.toml`, found by walking up from the current directory, holds
  the bench's settings: build and toolchain paths, the webcam, Line In,
  card UUIDs, Bluetooth addresses, and each player's own keys
  (`[targets.NAME]`, listed in `arb NAME --help` and in the target class's
  docstring). Personal values go there, never in code.
- `uv sync` in auto-rockbox, then run `.venv/bin/arb` (or `uv run arb`)
  from the port's checkout.
- `arb targets` lists the players and their capabilities. A command
  appears for a player only when the player has what it needs (a dev shell,
  a framebuffer read, mass storage, ...).
- Start the bench services once per session if the checks need them:
  `arb webcam serve` (a frame a second) and `arb linein serve` (a second of
  Line In at a time). They publish into rings that every run and analysis
  reads.

## One run

```
arb TARGET run build/TARGET/rockbox.elf 60 --keys "9000 D D S" --shots build/shots
```

1. The charger guard reads the battery and stops charging at the
   configured limit. Every device command does this.
2. The image goes onto the player (only the changed sectors are written) with
   a run config: the return time, the key script and any `--set KEY=VALUE`
   settings the dev build reads.
3. The player boots, runs the keys, and after SECONDS returns to its
   flashing mode by itself, with its log in RAM that the return keeps.
4. `arb` prints the log. With `--shots`, webcam frames from the run, the
   framebuffer and a contact sheet of both are saved in the directory.

Exit status 3 means the player did not come back in time: a hang, a crash
before the log, or a power-off. Read `arb TARGET log` (and on the YP3
`arb yp3 fault` and `arb yp3 threads`) before running anything else,
because the next run overwrites them.

Key scripts: `arb TARGET keys "SCRIPT"` checks a script and says how long it
takes. Numbers wait that many ms, letters press keys, and `+` holds (see
`auto_rockbox/keys.py` and the target's letters).

With `--usb`, the dev build starts USB and its dev shell where the player
has one: `arb TARGET sh CMD` sends one command. On the YP3,
`arb yp3 run IMAGE 0 --usb` keeps it up (no return), and `arb yp3 shot`
takes a screenshot over the shell. A sleeping LCD shows its last frame, so
send a key first.

## Reading results

- **The log** first. Fault and panic lines, `LOST` (no return) and an early
  return are all failures. Check the lines you expected, too.
- **The camera against the framebuffer.** Panel faults (init, timing,
  washed out) never show in the framebuffer. Compare the contact sheet's two
  columns.
- **Line In**: `arb linein analyse 30` gives the level and tone per second
  for the last 30 s, and `arb linein gaps` gives the dropouts. Capture
  stamps run about 3 s behind the player, so shift the window when you line
  up events. Keep the PC's Line In at unity gain.
- **The card**: `arb TARGET card-run 'ls -R "$MP"'`, `card-push rockbox.zip`
  and `card-check` boot the build as USB mass storage, mount the card and
  run the command, then the player returns.

## Habits

- Before each hardware step, write down what it tests and why.
- One process per device. The tools lock each player. Don't kill other
  tools by pattern to free a device: wait, or stop that process by its PID.
- Never leave a player charging unattended in its flashing mode. The guard
  stops the charger after each run, but a player left in the boot ROM for
  hours is not guarded.
- Build variants into a new directory. A long run reflashes its image on
  every recovery, so rebuilding over it changes what the run tests.
- Every crash or open experiment goes into the player's backlog
  (`docs/PLAYER/TODO.md`) at once.
- A check worth running twice is a test: a scenario in `suites/` (or the
  player's module when only it has the hardware), not a script
  ([testing.md](testing.md), Writing scenarios). `arb TARGET test` after
  a change, before a long run.

## Adding commands

Player-only commands live in `targets/NAME/commands.py`. Workspace-only
commands go in a Python file named in the port's
`[plugins] modules`. Both use the same decorators:

```python
from auto_rockbox.registry import arg, command, group

@command("dump-otp", target={"ts17"}, help="read the OTP words",
         args=[arg("out", type=Path)])
def dump_otp(ctx, args): ...

group("nor", help="the NOR flash")
@command("nor dump", target={"ts17"}, help="NOR to a file", args=[...])
def nor_dump(ctx, args): ...
```

Raise an `auto_rockbox.errors.ArbError` subclass for a failure that the
user should see as one line. `ARB_TRACEBACK=1` shows the traceback.
