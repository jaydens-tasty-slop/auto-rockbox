# Adding a player to auto-rockbox

A player is two halves that agree on a few memory layouts: the **device
side** in the port's dev build, and the **host side**, a `Target` subclass
in Python. The generic commands (`run`, `log`, `fb`, `keys`, `sh`,
`battery`, the card commands, soak, torture) then work for the new player
unchanged.

Start from the player that is closest to yours: `targets/ts17` loads over a
boot ROM download protocol into RAM and NOR, and `targets/yp3` flashes
through a ROM USB payload.

## Device side: the dev build contract

Most of it is shared Rockbox code, the same on every player: the log ring,
key scripts, thread traces and the serial mirror (`firmware/export/devlog.h`,
`firmware/devlog.c`), and the boot tasks and dev shell
(`apps/dev_shell.c`). A port's dev build defines `HAVE_DEVLOG` in its config
header behind its own switch (`YP3_DEV`, `TS17_DEV`), so the release build
does not change at all, and supplies a small `dev-<player>.c`:

**Run config.** `key=value` lines that the host writes where the image finds
them at boot; the image takes them once (clearing the magic), so a later
normal boot ignores them. `dev_config(key, buf, size)` answers from them
(`devlog_config_find()` parses the text). The shared keys are `return`
(seconds, 0 = never), `keys` (up to 255 bytes), `usb=1|storage`, `play`,
`volume`, `bluetooth=off`, `ls`, `monitor`, `poweroff`, `crash`, `sdlog`;
a port may add its own. Pick a place that survives what the host does before
the boot: the TS17 uses a RAM block that a reset into download mode leaves
alone, the YP3 a flash sector read through XIP. Without a host config,
return after a default time (90 s on the TS17), so a dev build can never
strand the player.

**Log ring.** Place a header (magic, total bytes ever written, ring size)
and the ring in RAM that survives the return, and hand them to
`devlog_init()`. Keep them across reboots that had no host config (a crash
reboot then still shows what happened), and reset them when a new host
config arrives. Useful extras: the PC the last tick interrupted (shows where
a hang spun; `dev_trace_copy()`), and a fault record.

**Return to the host.** After the return time, go back to the mode the host
loads from: a reboot into download mode (TS17), or stopping the watchdog
feeds so the recovery watchdog resets into ROM USB (YP3). Log the line the
host looks for (`returning to download mode at tick N`), so an early return
reads as a hang. `dev_return()` does it at once (shell `rom`, `!rom`).

**Hooks.** `dev_feed()` (the recovery watchdog through long boot work),
`dev_code_address()` (for return-address scans), `dev_usec()`,
`dev_target_boot()` (the port's own boot tasks) and `dev_target_cmd()` (its
own shell commands, after the shared ones; NULL for none).

**Keys.** The button driver ORs `dev_held_buttons` into what it reads, so
a key script takes the same path as a finger. The grammar is shared
(`auto_rockbox.keys`): U D L R S, V v and P where the player has volume and
power keys; `+` `*` `~` suffixes. List the letters in the Target's
`key_letters`, so the host rejects typos before a flash.

**USB.** `usb_detect()` reports no cable until the run config asks for USB
(`dev_usb_enabled`). The dev build defines `USB_ENABLE_SERIAL`,
`USB_SERIAL_RX_HOOK dev_serial_rx`, and `USB_SERIAL_RX_WHOLE_PACKET` where the
USB driver drops an OUT packet longer than the request, and gives the
device its own USB IDs, so the host finds the shell's tty by them.

**Framebuffer.** Nothing to do if the host can read RAM after the return:
it finds the framebuffer through the ELF's symbol. Otherwise add a shell
`shot` command.

**Dev shell** (shared, once the port's USB works): Rockbox's USB serial
(CDC-ACM) in charge-only mode. One command per line; the reply is the log
lines the command writes, ended by a line `ok`, or by `err <command>`. The
host's `auto_rockbox.shell.Shell` speaks this; `help` lists the commands,
and `apps/dev_shell.c` has their reply formats.

**Recovery watchdog.** One switch (`*_RECOVERY_WATCHDOG`), on during
bring-up. The firmware arms it at its earliest entry, with the reason that
makes the ROM wait for USB. Then any hang comes back to the host without a
hand on the player.

## Host side: the Target

```python
from auto_rockbox.registry import register_target
from auto_rockbox.target import Lcd, RunRequest, Target

@register_target
class MyPlayer(Target):
    """SoC, flash, how it is reached.

    Config ([targets.myplayer] in auto-rockbox.toml):
        card_uuid   the card's filesystem UUID (default: the USB disk with
                    this player's IDs, usb_product)
    """
    name = "myplayer"
    summary = "XYZ123, 4 MiB NOR, ROM download mode"
    lcd = Lcd(128, 160)
    key_letters = "UDLRSP"
    capabilities = frozenset({"framebuffer", "shell", "battery", "usb", "storage"})
    return_line = "returning to the host"     # the dev build's log line at an on-time return
    dev_dir = "/mydev"                        # the build's DEV_CARD_DIR

    def launch(self, req: RunRequest) -> None: ...      # image + self.run_config(req), start it
    def wait_return(self, deadline, tick) -> bool: ...  # back in download mode?
    def read_log(self) -> str: ...                      # the unwrapped ring
    def shell(self) -> Shell:                           # the dev shell's tty, by USB IDs
        return Shell(self.name, "1209", "0003")
```

Put it in `src/auto_rockbox/targets/<name>/`, which is imported
automatically. A port that keeps its target out of tree can load it as a
plugin instead: an `auto_rockbox.plugins` entry point, or a file listed in
the workspace's `[plugins] modules`.

- **Capabilities** switch generic commands on: `framebuffer`
  (`read_framebuffer`), `shell` (`shell()`), `battery` (`battery()` plus a
  charger guard in `before_run`/`after_run`), `cold` (boots through the
  normal boot chain), `usb` (`RunRequest.usb` starts the dev shell),
  `storage` (`mass_storage()` yields the card's block device), `touch`
  (key scripts may touch the panel: `@X:Y`, `@X:Y>X2:Y2`, keys.py), `fm`
  (the dev shell's `radio` and `fmstat`, and the `then=` run setting: the
  soak's `fm-station` against the workspace's `[fm]` station), `mic` (a
  built-in mic the dev shell's `rec SECONDS NAME mic mono` records: the
  soak's `mic-record`), and `usb-poweroff` (the build powers off with USB attached,
  HAVE_POWEROFF_WHILE_CHARGING: the soak's power-off test can run on the
  bench).
- **Player-only commands**: `@command("nor", help=..., target={"myplayer"})`
  in the subpackage's `commands.py`. A player command of the same name as a
  generic one replaces it for that player (the YP3's `run` adds `--push` and
  `--modules`).
- **Tests come with the dev loop**: every player's soak scenarios
  (boot, crash, sdtest, tone, ...) run as soon as `launch`, `wait_return`
  and `read_log` work, judged by `return_line`; `arb myplayer test` lists
  what else needs a capability or the dev shell ([testing.md](testing.md)).
- **Games**: `games`, each MVP game's plugin under `.rockbox/rocks` with a
  key script that starts it from its menu, plays it and quits with the
  player's keys (the soak's `games`; a player without them skips it).
- **Soak scenarios of its own**:
  `@scenario("radio", suite="soak", area="fm", target={"myplayer"})`, for
  what follows its menus and key paths. Prefer a generic scenario that
  works on every player ([testing.md](testing.md), Writing scenarios).
- **Torture**: a `RockboxTorture` subclass in the subpackage's `torture.py`
  with `@player_suite("myplayer")` and the features its build has; the
  shared scenarios then run on it, with the card's dev folder from
  `dev_dir` ([testing.md](testing.md), Torture suites).
- **Nothing personal in code**: card UUIDs, Bluetooth addresses, backup image
  paths and tool paths come from `self.conf` (the `[targets.<name>]` table).
  Document each key in the class docstring with its default.
- **Protocol facts** (addresses, magics, commands) are named constants with
  a one-line comment saying where they come from.
- **Built helpers** (payloads, gadgets) ship as sources in the subpackage
  and are built on demand into `ws.build_dir / "arb" / <name>`.
- **Locks**: `Target.lock()` serialises device access across processes.
  Hold it around every transfer, never across a whole run that another tool
  might need to interrupt.

Check with `uv run ruff check src`, `uv run ty check`, the target's tests
under `tests/<name>/`, and then one real `arb <name> run` with `--shots`,
comparing the contact sheet's framebuffer with the camera.
