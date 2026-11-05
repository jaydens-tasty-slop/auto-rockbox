# Adding a player to auto-rockbox

A player is two halves that agree on a few memory layouts: the **device
side** in the port's dev build (Rockbox code behind the port's `*_DEV`
define), and the **host side**, a `Target` subclass in Python. The generic
commands (`run`, `log`, `fb`, `keys`, `sh`, `battery`, the card commands,
soak, torture) then work for the new player unchanged.

Start from the player that is closest to yours: `targets/ts17` loads over a
boot ROM download protocol into RAM and NOR, and `targets/yp3` flashes
through a ROM USB payload and has the serial dev shell.

## Device side: the dev build contract

Put all of it behind one define (`YP3_DEV`, `TS17_DEV`) so the release build
does not change at all.

**Run config.** The host writes it where the image finds it at boot, and the
image consumes it once (clearing the magic), so a later normal boot ignores
it. It holds at least:

- the return time in seconds (0 = never),
- a key script (up to 239 bytes),
- flags or `key=value` settings: start the USB stack, dump threads before
  returning, play a file, and so on.

Pick a place that survives what the host does before the boot. The TS17
uses a RAM block that a reset into download mode leaves alone: a binary
struct with magic "TSDC". The YP3 uses a flash sector holding `key=value`
text, read through XIP. Without a host config, return after a default time
(90 s on the TS17), so a dev build can never strand the player.

**Log ring.** `kind: text` lines into a byte ring in RAM that survives the
return: a header with a magic, the total bytes ever written and the ring
size, then the ring. The host reads it after the return and unwraps it.
Keep it alive across reboots that had no host config (a crash reboot then
still shows what happened), and reset it when a new host config arrives.
Useful extras: the last interrupted PCs from the tick (shows where a hang
spun), a thread snapshot, and a fault record with a CRC and a commit word
written last.

**Return to the host.** After the return time, go back to the mode the host
loads from: a reboot into download mode (TS17), or stopping the watchdog
feeds so the recovery watchdog resets into ROM USB (YP3). Log the line the
host looks for (`returning to download mode at tick N`), so an early return
reads as a hang.

**Key script.** Tokens separated by spaces or commas:

- a number waits that many milliseconds;
- a run of letters presses those keys together, for a short tap;
- a suffix changes the press (`+` holds 1 s, `++` 2 s; the YP3 also has `*`
  for a real long press and `~` for a real short tap).

Inject at the button driver's level (OR the scripted state into what the
driver reads), so the script exercises the same path as a finger. Log
`keys: done` at the end, and `keys: bad token at '...'` and stop on a bad
token. List your letters and suffixes in the Target's `key_letters` and
`key_suffixes`, so the host rejects typos before a flash.

**Framebuffer.** Nothing to do if the host can read RAM after the return:
it finds the framebuffer through the ELF's symbol. Otherwise add a shell
`shot` command.

**Dev shell** (once USB works): Rockbox's USB serial (CDC-ACM) in
charge-only mode. One command per line; the reply is the log lines the
command writes (mirrored live to the serial port), ended by a line `ok`, or
by `err <command>` on failure. The host's `auto_rockbox.shell.Shell` speaks
this. Send nothing until the host has written since the last bus reset (a
tty in cooked mode would echo the output back as input). Handle `!rom`
(return now) in the receive hook, so it works while a command blocks. The
torture suite needs `health`, `msgs`, `status`, `keys`, `play`, `stop`,
`vol`, `log`, `ls`, `cfg`, `run`, `bat` and `!rom`; see the YP3's
`apps/yp3_dev.c` for their reply formats.

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
        card_uuid   the card's filesystem UUID (default: any Rockbox USB disk)
    """
    name = "myplayer"
    summary = "XYZ123, 4 MiB NOR, ROM download mode"
    lcd = Lcd(128, 160)
    key_letters = "UDLRSP"
    capabilities = frozenset({"framebuffer", "battery", "usb", "storage"})

    def launch(self, req: RunRequest) -> None: ...      # image + run config, start it
    def wait_return(self, deadline, tick) -> bool: ...  # back in download mode?
    def read_log(self) -> str: ...                      # the unwrapped ring
```

Put it in `src/auto_rockbox/targets/<name>/`, which is imported
automatically. A port that keeps its target out of tree can load it as a
plugin instead: an `auto_rockbox.plugins` entry point, or a file listed in
the workspace's `[plugins] modules`.

- **Capabilities** switch generic commands on: `framebuffer`
  (`read_framebuffer`), `shell` (`shell()`), `battery` (`battery()` plus a
  charger guard in `before_run`/`after_run`), `cold` (boots through the
  normal boot chain), `usb` (lets Rockbox start USB), and `storage`
  (`mass_storage()` yields the card's block device).
- **Player-only commands**: `@command("nor", help=..., target={"myplayer"})`
  in the subpackage's `commands.py`. A player command of the same name as a
  generic one replaces it for that player (the YP3's `run` adds `--push` and
  `--modules`).
- **Soak scenarios**: `@scenario("tone", suite="soak", target={"myplayer"})`.
  Menu layouts and key paths are player specific, so the soak's scenarios
  live with the target.
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
