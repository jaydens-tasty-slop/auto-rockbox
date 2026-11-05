# auto-rockbox

`arb` is a dev loop and test bench for porting [Rockbox](https://www.rockbox.org)
to cheap MP3 players. It flashes a build onto a player, runs it with a
scripted key sequence, waits for it to come back to the PC by itself, and
reads the log, the framebuffer and a webcam view of the panel. On top of that
loop it runs long soak and torture runs, checks the audio on Line In,
measures audio quality against a reference device, runs battery and
Bluetooth range tests, and handles the player's SD card.

It came from two ports: the **YP3** (SL6801, two Cortex-M4 cores, 2 MiB NOR
flash) and the **TS17** (Actions Lark, Cortex-M4F, 4 MiB NOR flash). Each
player is one `Target` class. Everything else is shared, so a new port
writes its transport and gets the rest.

## Install

```
git submodule add https://github.com/jaydens-tasty-slop/auto-rockbox
cd auto-rockbox && uv sync          # extras: --extra bt (Bluetooth peers), --extra plots
```

Put an `auto-rockbox.toml` at the root of the port's checkout (see
`src/auto_rockbox/workspace.py` and each target's docstring for the keys),
then run `auto-rockbox/.venv/bin/arb` from anywhere inside the checkout.

## Use

```
arb targets                                     # the players and what they can do
arb yp3 run build/yp3/rockbox.elf 60 --keys "S 1500 D S" --shots build/shots
arb ts17 soak build/ts17/rockbox.elf build/soak --hours 8 --shots
arb linein serve &                              # Line In, one second at a time
arb linein analyse 30                           # level and tone of the last 30 s
arb bench curve build/battery/battlog.csv --from-mv 4075
arb fm-check rec.wav
```

`arb TARGET --help` and `arb COMMAND --help` list everything.

## Skills

The `skills/` directory has the method behind the tools, in plain markdown:

- [bring-up.md](skills/bring-up.md): from an undocumented player to a
  shippable Rockbox.
- [adding-a-target.md](skills/adding-a-target.md): what a dev build must
  do, and the `Target` class.
- [dev-loop.md](skills/dev-loop.md): everyday `arb`.
- [testing.md](skills/testing.md): soak and torture runs.
- [audio-quality.md](skills/audio-quality.md),
  [fm-radio.md](skills/fm-radio.md),
  [bluetooth-range.md](skills/bluetooth-range.md),
  [battery.md](skills/battery.md).
- [release.md](skills/release.md): what a release must pass.

## Targets

| Target | Player | Reached through |
|---|---|---|
| `yp3` | SL6801 "YP3" | the boot ROM's USB recovery mode (smartlink_flash's `smtlink_dump`) and a small service payload |
| `ts17` | Actions Lark "TS17" | the boot ROM's ADFU download mode (pyusb) and an on-CPU gadget |

A port with its target in its own tree can send it here as a pull request
once it has shipped a release (see the end of
[release.md](skills/release.md)).

## Development

```
uv run ruff check src tests
uv run ty check
uv run python -m tests
```

CI runs the same checks on every pull request. Tests that need a Rockbox
build, a toolchain or a player skip themselves.

## Licence

GPL-2.0-or-later, like Rockbox. No stock firmware or flash dumps are in this
repository. What came from studying stock firmware is register facts, init
values and data tables, and the register sequences the host-side payloads
follow.
