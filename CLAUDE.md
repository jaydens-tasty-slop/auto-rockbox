# auto-rockbox

A typed Python suite (`arb`) for porting Rockbox to MP3 players: the dev loop
(flash or load, run, come back, read the log, framebuffer and webcam), soak
and torture runs, Line In and audio-quality analysis, battery runs and card
handling. One `Target` subclass per player; everything else is shared.

## Skills (read the one that fits the task)

- [skills/bring-up.md](skills/bring-up.md): bringing Rockbox up on a new
  player, from no documentation to a product. Start here.
- [skills/adding-a-target.md](skills/adding-a-target.md): the dev-build
  contract on the device and the `Target` class on the host.
- [skills/dev-loop.md](skills/dev-loop.md): day-to-day use of `arb`.
- [skills/testing.md](skills/testing.md): soak and torture runs, Line In
  checks, reading failures.
- [skills/audio-quality.md](skills/audio-quality.md): measuring the output
  against a reference device, calibrating levels, fitting an EQ.
- [skills/fm-radio.md](skills/fm-radio.md): tuner, reference clock,
  capture path, RDS, and the traps they hide.
- [skills/bluetooth-range.md](skills/bluetooth-range.md): walks with the
  headset and with the player, against stock.
- [skills/battery.md](skills/battery.md): battery runs and discharge tables.
- [skills/release.md](skills/release.md): what a release must pass.

## Code

- `src/auto_rockbox/`: core (`target.py`, `registry.py`, `run.py`,
  `engine.py`, `shell.py`, `workspace.py`), `commands/` (generic commands),
  `suites/` (soak, torture), `targets/<player>/` (transport, player-only
  commands and scenarios). Every module in those packages is imported, and
  its decorators register what it defines.
- Workspace settings (paths, card UUIDs, Bluetooth addresses, webcam focus)
  live in the port's `auto-rockbox.toml`, never in code.

## Rules

- `uv run ruff check src` and `uv run ty check` stay clean (ty fails on
  warnings). Python 3.12, typed, no dead code, no one-off probes left
  behind.
- One process per device: the tools lock each device. Never kill other
  processes by pattern (`pkill`, `killall`).
- No stock firmware bytes, dumps or code derived from stock in this
  repository. Register facts and data tables are fine.
- Before a hardware step, write down what it tests and why.
