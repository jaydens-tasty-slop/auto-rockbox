"""The torture of a Rockbox dev build through the dev shell every player's
build shares (apps/dev_shell.c): its commands and replies, the health line's
Rockbox fields, what every step is checked for, and the scenarios every
player runs (suites/torture.py runs them). A player's subclass of
RockboxTorture (`@player_suite("NAME")`, targets/NAME/torture.py) says where
its dev folder is and adds what only its own shell or hardware has.

    arb yp3 torture build/torture-elf/rockbox.elf build/torture/RUN --hours 6

The card is set up as for the dev loop (the port's docs/testing.md, Card
setup): /codectest/ test files (`arb media-codectest`), /stress/ and
"/Playlists/Stress test.m3u8" (`arb media-stress`), /.rockbox/rocks/ plugins
and the codecs built with the core, a music library (artist/album folders;
library scenarios skip without one), a built database, the cabbiev2 theme,
/video/ videos (`arb media`'s video/), and tone.mp3 in the player's dev
folder (`card_dir`, the build's DEV_CARD_DIR; `arb media-tone`: 1 kHz left,
1.5 kHz right, 12 dB down after 10 s). The settings files the steps load
(cfgs()) go into the dev folder before the run's first boot, with the player
as USB mass storage (capability "storage"; put_card_files), unless the
player puts them there at every boot.

After every step: the messages it put on the screen (`msgs`: splashes, yes/no
prompts, plugin errors) fail it when they read as errors (ERROR_MSG, less the
scenario's `expected_msgs` and the plugins known to fail without their data);
a track played from its start must decode to the same hash as earlier plays
of that file over as many frames; a Video Player play must not underrun,
drop over VIDEO_DROP_MAX of its pictures or lose its second core; with a
charge limit, the charger must be off above it; a database build must end
before the next step.

What players differ in is declared by their subclass:

    card_dir       the dev folder on the card (DEV_CARD_DIR); required
    main_menu      the main menu's items in order (root_menu.c)
    slow_codecs    /codectest file name prefixes known too slow or over a limit
    expected_msgs  per scenario, the error messages it provokes on purpose
    charge_limit   % set at each boot (t_limit.cfg; HAVE_CHARGE_LIMIT builds)
    features       what the build has that scenarios need (needs=):
                   lcd-gate      gate_on() reads the LCD controller's clock
                   fm-tune       fm_tune() tunes without the radio screen
                                 (FM recordings)
                   bt-hid        Bluetooth game controllers and keyboards
                                 (HAVE_BLUETOOTH_HID)

and hooks: boot_settings, put_card_files, prepare, gate_on, fm_tune,
bt_link_table, bt_fail_details, and TortureSuite's records, wait_host and
expected_return.

The bt-* scenarios make this PC the player's headset, through BlueZ, with
the player's stream on a null sink for the whole run (never the speakers).
They are in a run only with the player's address configured, and need it
paired with this PC (`arb bt-peer pcsink`) and a Bluetooth build:

    [targets.NAME]
    bt_address = "AA:BB:CC:DD:EE:FF"    # the player's BD_ADDR
    [bluetooth]
    sink = "arb_null"               # the null sink its stream goes to (made if missing)
    adapter = "usb:VID:PID"         # this PC's adapter for Bumble (bt-headset, bt-hid)

`bt-pcpower` (this PC's adapter off and on: it once wedged until bluetoothd
restarted) is off unless --weights gives it a weight. bt-headset and bt-hid
lend the adapter to a Bumble peer (`arb bt-peer pcheadset`, `arb yp3
bt-peer hid`; `uv sync --extra bt`) with keys of its own: both sides forget
theirs first, and the next step pairs the player with this PC again
through a bluetoothctl agent.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from argparse import Namespace
from collections import Counter
from collections.abc import Callable, Generator, Iterable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from .. import card, fb, linein, media
from ..bt import bluez
from ..btpeer import analysis, have_bumble, pcsink
from ..btpeer.options import HeadsetOptions, HidOptions
from ..errors import ArbError
from ..registry import Context, arg, command
from ..shot import screenshot
from ..target import RunRequest, Target
from .torture import Health, Lost, Records, Register, TortureError, TortureSuite, scenario

# Suite features (Suite.features)
ROCKBOX = "rockbox"             # the suite is a RockboxTorture: every scenario here needs it
LCD_GATE = "lcd-gate"           # gate_on() reads the LCD controller's clock gate
FM_TUNE = "fm-tune"             # fm_tune() tunes without the radio screen
BT_PC = "bt-pc"                 # this PC can be the player's headset
BT_BUMBLE = "bt-bumble"         # and its adapter can be lent to a Bumble peer
BT_HID = "bt-hid"               # the build takes Bluetooth input devices

# The card (the dev folder's files are the suite's: card_path())
CFGS = {
    "t_sleep.cfg": "backlight timeout: 3\nbacklight timeout plugged: 3\n"
                   "lcd sleep after backlight off: 2\n",
    "t_wake.cfg": "backlight timeout: 15\nbacklight timeout plugged: 15\n"
                  "lcd sleep after backlight off: 5\n",
    # English by name: the built-in one is "lang: " (no file), and a .cfg
    # with that keeps whatever language a crawl selected, so t_base.cfg's
    # restore left the menus in Chinese (TS17 torture 10). The main menu in
    # its default order (MAIN_MENU, menu() walks by index): random keys in
    # Settings > Main Menu reorder or hide items, and the card keeps that
    # (TS17 torture 29: Database went to Settings)
    "t_default.cfg": "wps: -\nsbs: -\nfms: -\niconset: -\nviewers iconset: -\n"
                     "backdrop: -\nforeground color: FFFFFF\nbackground color: 000000\n"
                     "selector type: bar (gradient)\nstatusbar: top\nui viewport: -\n"
                     "lang: /.rockbox/langs/english.lng\nroot menu order: -\n",
}
THEMES = ("/.rockbox/themes/cabbiev2.cfg", "/.rockbox/themes/rockbox_failsafe.cfg")
CODECTEST = f"/{media.CODECTEST_DIR}"            # `arb media-codectest`
CODEC_EXT = re.compile(r"\.(mp3|flac|m4a|tta|aiff|au|wav|wv|ac3|spx|ape|ogg|opus)$")
PROBE = f"{CODECTEST}/flac_16_44.flac"          # an SD codec: are the modules this build's?
UNSUPPORTED = f"{CODECTEST}/wma_320.wma"        # WMA: no codec on master
STRESS = "/stress"                              # `arb media-stress`
STRESS_PLAYLIST = f"/{media.STRESS_PLAYLIST}"
STRESS_RATES = {48000: re.compile(r"_48k?\."), 44100: re.compile(r"_44[._]")}   # by name
ROCKS = "/.rockbox/rocks"
PLUGIN_DIRS = ("games", "apps", "demos")
# xrick: without Rick Dangerous's data files (nobody ships them) it only
# splashes an error and returns OK, which KNOWN_PLUGIN_ERRORS cannot match
PLUGIN_SKIP = ("battery_bench.rock", "lastfm_scrobbler.rock", "xrick.rock")
VIDEO_DIR = "/video"
VIDEO_PLAYER = f"{ROCKS}/viewers/videoplayer.rock"
VIDEO_CFG = f"{ROCKS}/viewers/videoplayer.cfg"     # its settings, next to it
VIDEO_EXT = (".avi", ".mkv", ".mp4", ".mov")
RECORDINGS = "/recordings"
CARD_S = 180                    # a mass storage boot that puts the settings files on the card
LIBRARY_DEPTH = 3               # folder levels library_track() looks in, the root's included
LIBRARY_LS = 12                 # and the listings it may take

# The player
MAIN_MENU = ("files", "database", "resume", "settings", "recording", "fm",
             "playlists", "plugins", "system", "shortcuts")    # root_menu.c's order
# enum current_activity (misc.h), `health` act
ACT_MAINMENU, ACT_RECORDING, ACT_FM, ACT_DATABASE, ACT_PLUGIN = 1, 3, 4, 8, 14
ACT_NAMES = ["?", "main menu", "WPS", "recording", "FM", "playlist viewer",
             "settings", "files", "database", "plugins", "quickscreen", "pitch",
             "option select", "playlists", "plugin", "context menu", "system",
             "time/date", "bookmarks", "shortcuts", "id3 info", "usb"]
AUDIO_PLAYING, AUDIO_PAUSED = 1, 3          # audio_status()
FM_KHZ = (98100, 101500, 104300, 92500, 88700)
VOLUME_DB = -20
SHELL_LINE = 127                # apps/dev_shell.c sh_rx[128]: longer lines are cut
SEL_TRIES = 4                   # corrections a menu walk makes at one level
SEL_SETTLE_S = 0.4              # `sel` unchanged this long: the UI took its keys
SEL_READS = 10                  # and how many reads wait for that
SEL_ITEM_MAX = 47               # `sel` item text kept (dev_shell.c drawn_list.item)
DB_COMMIT_DELAYED = "Please reboot to enable"   # LANG_PLEASE_REBOOT, root_menu.c

# A message (splash, prompt) that reports a failure ("No files" is an empty
# list's notice, as an empty Shortcuts menu shows; "No file" is not)
ERROR_MSG = re.compile(r"(?i)error|fail|not enough|memory|cannot|can't|couldn't|unable|"
                       r"invalid|corrupt|incompatible|wrong|not found|unsupported|leak|"
                       r"denied|too (big|large|long)|no file(?!s)|panic|bad ")
# What random keys provoke on any player: they accept an empty (or "/", "..")
# name in a keyboard, which upstream refuses with FORC_UNKNOWN_FAILURE
# (apps/fileop.c prompt_name()); a failing mkdir or rename reports ten times
# its error, never -1
RANDOM_KEY_MSG = re.compile(r"(?i)^(create directory|rename) failed \(-1\)$")
SHARED_EXPECTED_MSGS = {"monkey": RANDOM_KEY_MSG, "crawl": RANDOM_KEY_MSG}
# Plugins that end in "Plugin returned error" when started without their data
# or a file (upstream behaviour): the splash that explains it, None if none
KNOWN_PLUGIN_ERRORS: dict[str, str | None] = {
    "dict.rock": r"Failed to open index",                      # no dictionary
    "xworld.rock": r"data files missing|res_allocMemBlock",     # no game data
    "mp3_encoder.rock": None,                                   # a .wav viewer
    "imageviewer.rock": r"No supported files|No file|Unsupported file",
    "random_folder_advance_config.rock": r"folder_advance_list",
}
# Plugins that refuse every way out in a state random keys can leave them in
# (upstream behaviour), and the splash that says so: Sudoku keeps a board
# with a conflict, its quit and its menu both check (TS17 sweep, step 492)
KNOWN_EXIT_REFUSALS: dict[str, str] = {
    "sudoku.rock": r"^Illegal move!$",
}
# Error splashes a plugin shows, as upstream does, for an action random keys
# reach: Key Remap will not save a keymap with fewer than four entries, and
# the one it starts with is empty (keyremap_save_current(); TS17 sweep 28)
KNOWN_PLUGIN_SPLASHES: dict[str, str] = {
    "keyremap.rock": r"^Error Saving$",
}
CODEC_ERRORS = re.compile(r"codec load \d+ failed|ov_open failed|codec header error|"
                          r"no matching image|EOM|not enough memory")
VIDEO_DROP_MAX = 0.05           # the Video Player's dropped share of due pictures
DECODE_FRAMES = 200000          # a track's hash covers its first this many frames
LINEIN_STEADY_DB = 1.0          # the tone's level within a step
LINEIN_DB = 2.0                 # ...and against earlier steps
LINEIN_ONSET_S = 6              # the tone is heard within this of the play command
FOLDER_BUDGET_S = 3             # next/previous folder plays within this
FOLDER_WAIT_S = 20              # ...and is waited for this long
# Keys (`keys`): + held 1 s, * held 1.2 s from the live key state; UL = Home
MONKEY_KEYS = ["U", "D", "L", "R", "S", "U+", "D+", "L+", "R+", "S+", "100", "400"]
MONKEY_WEIGHTS = [5, 5, 5, 5, 4, 1, 1, 1, 1, 1, 2, 2]
# Out of a plugin: ported games' pause gesture (Select+Up held) then Quit, the
# last item (Up wraps to it), again for a start menu, once more after a banner
# (games clear the key queue between levels); menus, long Select, Left; out of
# a keyboard (long Left) and Text Editor's unsaved-changes menu; the
# puzzles' pause, Select+Down tapped (sgt-puzzles' BTN_PAUSE on YP3_PAD;
# held, it would spill into the menu), then Quit; Quit, last, in a menu that
# keeps its last pick and ignores Back (Bubbles: Up wraps to the end only
# from the top; a held key stops at the end, its repeats never wrap), from
# up to two levels down (Codebuster: an option under its Settings); a long
# Select menu that opens on its second item with Quit two below (MazezaM's,
# on Retry level); two long Selects, Left, then Quit, the last item, Down
# held 2 s (Xobox on YP3_PAD: a Select release toggles its pause and long
# Select opens its menu only unpaused, so the first may only resume; a list
# ignores the second, TS17 sweep step 496. MazezaM's exit opens it unpaused
# and picks Speed, where Left cancels; in the menu Left only reopens it, at
# the item it kept, from which Down's repeats stop on Quit: TS17 sweep 28
# step 492, every pass from that state); last, the Select+Left chord three times
# (Rockpaint's quit on YP3_PAD and the only way out of its dialogs; a
# half-drawn shape takes one press more); the YP3 pad's menu chord, Up+Left,
# then Quit, the last item (PictureFlow, where Left scrolls the covers and is
# never Back); then Up+Left twice (RockTrack: its editors to its start menu,
# then out); last, Left, Up and Select, then Up held (LRC Player: long Select
# opens its Timetag Editor, which only Left leaves, through "Save Changes?"
# when a tag moved, where Up wraps to Discard; its lyrics screen quits only
# on Up held, Now Playing's Stop; TS17 sweep 28 step 15); Select+Down, then
# the pause gesture and Quit (Terrario: a Down tap in play opens its
# inventory, which only Select+Down leaves, and the chords before reopen
# it; TS17 sweep 28 step 475)
PLUGIN_EXITS = ["SU* 800 U S", "U S", "4000 SU* 800 U S", "U S", "U S L S+ U S L L",
                "L+ S+", "U+ S", "SD 800 U S", "D S U S", "L 300 L 300 D+ S",
                "L+ 300 L 300 U S",
                "S+ 300 D 300 D 300 S", "S+ 300 S+ 300 L 300 D++ S",
                "SL 300 SL 300 SL", "UL 500 U S", "UL 300 UL", "L 300 U S 300 U+",
                "SD 800 SU* 800 U S"]
# Before Home: Back presses, then plugin exits, for screens that ignore Home
HOME_FIRST_KEYS = ["", "L 300 L 300 L 300 ", "SU* 800 U S ", "U S L S+ U S L L ", "L+ S+ ",
                   "U+ S "]
# The crawl: menu items it never runs (they lose data, power off or need hours)
CRAWL_SKIP = re.compile(r"(?i)shut ?down|power off|reboot|reset|format|delete|erase|"
                        r"clear|initiali[sz]e|rebuild|update now|export|import|"
                        r"usb|write .*cfg|save .*(theme|settings)")
CRAWL_KEYS = ["U", "D", "L", "R", "S", "U+", "D+", "L+", "R+", "S+", "UL"]

# Bluetooth
BT_CONNECT_S = 25               # connect and reconnect deadline
BT_SCAN_S = 30                  # the player's inquiry and name requests end within
BT_GONE_S = 30                  # link loss seen (supervision timeout 20 s)
BT_SETTLE_S = 4                 # audio routing follows a status change
BT_OFF, BT_ON, BT_CONNECTED, BT_STREAMING = range(4)    # enum bt_status
SINK_BUILTIN, SINK_BT = 0, 1    # enum pcm_sink_ids
BT_BITPOOL_OK = 47              # the stream to this PC climbs back to this...
BT_BITPOOL_S = 45               # ...within this (at the desk: 35 -> 53 in ~25 s)
AVRCP = ["Play", "Pause", "Next", "Previous", "VolumeUp", "VolumeDown", "Stop",
         "FastForward", "Rewind"]
AVRCP_WEIGHTS = [4, 4, 3, 3, 2, 2, 1, 1, 1]
PLAYER_NAME = "Rockbox"         # the player's Bluetooth name starts with this
CAPTURE_S = (4, 6)              # bt-capture records this many s of the tone's loud part,
CAPTURE_FROM_MS = (1500, 3000)  # starting this far into it (its start was heard by then)
CAPTURE_GAP_MS = 10             # a shorter dip is not a dropout
TONE_HZ = (1000.0, 1500.0)      # the tone file's left and right channels
TONE_HZ_TOL = 0.02
HEADSET_CYCLE_S = 120           # a bt-headset cycle's limit (plus HEADSET_SPARE_S)
HEADSET_SPARE_S = 60
HID_LIMIT_S = 300
HID_KEYS = ["up", "down", "left", "right", "b", "home"]     # never A: it selects
HID_DS4_KEYS = ["l1", "r1", "l2", "r2"]


# ---------------------------------------------------------------- replies

@dataclass(frozen=True)
class Status:
    """`status`: audio_status() (-1: no line), elapsed and length in ms, the
    rate and the track's path ("-" for none)"""

    audio: int
    elapsed: int
    length: int
    hz: int
    path: str


def parse_status(lines: Iterable[str]) -> Status:
    for ln in lines:
        if m := re.match(r"audio (\d+) elapsed (\d+)/(\d+) ms (\d+) Hz .* file (.*)$", ln):
            return Status(int(m[1]), int(m[2]), int(m[3]), int(m[4]), m[5])
    return Status(-1, 0, 0, 0, "-")


def progress_ms(s1: Status, s2: Status) -> int:
    """How far playback moved from S1 to S2: across a track change (to the
    next track, or the same one again) the rest of the first plus the second's"""
    if s2.path != s1.path or s2.elapsed < s1.elapsed:
        return s1.length - s1.elapsed + s2.elapsed
    return s2.elapsed - s1.elapsed


def parse_msgs(lines: Iterable[str]) -> tuple[int | None, list[tuple[str, str]]]:
    """`msgs AFTER`: the next number (None: a build without `msgs`) and each
    message as (kind, text); kind "lost" with the count of those the
    player's list no longer held"""
    nxt, out = None, []
    for ln in lines:
        if m := re.match(r"msg \d+ (\w+) (.*)$", ln):
            out.append((m[1], m[2]))
        elif ln.startswith("msgs lost "):
            out.append(("lost", ln.split()[-1]))
        elif ln.startswith("msgs next "):
            nxt = int(ln.split()[-1])
    return nxt, out


@dataclass(frozen=True)
class Sel:
    """`sel`: the list drawn last"""
    index: int      # its selected item, from 0
    count: int
    draws: int      # lists drawn so far
    item: str       # the selected item's text (SEL_ITEM_MAX characters)


def parse_sel(lines: Iterable[str]) -> Sel | None:
    """`sel` "sel N/COUNT draw D | TITLE | ITEM" (None: a build without it)"""
    for ln in lines:
        if m := re.match(r"sel (-?\d+)/(\d+) draw (\d+) \|.*\|(.*)$", ln):
            return Sel(int(m[1]), int(m[2]), int(m[3]), m[4].strip())
    return None


def message_problems(msgs: list[tuple[str, str]],
                     *expected: re.Pattern[str] | None) -> list[str]:
    """The messages that read as errors, but those EXPECTED (what the step's
    scenario provokes) and the known plugin errors (a plugin's "returned
    error" and the splash before it)"""
    known = [why for kind, text in msgs if kind == "plugin" and "returned error" in text
             for rock, why in KNOWN_PLUGIN_ERRORS.items() if f"/{rock} " in text]
    bad = []
    for kind, text in msgs:
        if kind in ("decode", "video", "lost") or not (kind == "plugin" or ERROR_MSG.search(text)):
            continue
        if any(e and e.search(text) for e in expected):
            continue
        if known and (any(f"/{r} " in text for r in KNOWN_PLUGIN_ERRORS)
                      or text == "Plugin returned error"
                      or any(w and re.search(w, text) for w in known)):
            continue
        bad.append(f'{kind} "{text}"')
    return bad


def parse_battery(lines: Iterable[str]) -> tuple[int, int, bool] | None:
    """`bat`: (mV, %, charging)"""
    for ln in lines:
        if m := re.match(r"battery (-?\d+) mV (-?\d+)% charging=(\d) power=(\d)", ln):
            return int(m[1]), int(m[2]), m[3] == "1"
    return None


def ls_entries(lines: Iterable[str]) -> list[str]:
    """`ls DIR`: the entries' names, "NAME/" for a folder (lines "  NAME
    SIZE"; a name with ':' is a log line in between, no FAT name)"""
    names = [ln[2:].rsplit(" ", 1)[0] for ln in lines if ln.startswith("  ") and " " in ln[2:]]
    return [n for n in names if ":" not in n and "\n" not in n]


def r32_words(lines: Iterable[str]) -> list[int]:
    """`r32 ADDR N`: the words, from lines "ADDR: VALUE" (hex)"""
    return [int(m[1], 16) for ln in lines if (m := re.match(r"[0-9a-f]{8}: ([0-9a-f]{8})$", ln))]


@dataclass(frozen=True)
class MenuNode:
    path: str       # Down presses per level after Home, e.g. "3.2.1"
    kind: str       # M menu, S setting, F function, V value, ...
    name: str


def parse_menutree(lines: Iterable[str]) -> list[MenuNode]:
    """`menutree`: lines "mt PATH TYPE NAME [CFG]\""""
    return [MenuNode(m[1], m[2], m[3]) for ln in lines
            if (m := re.match(r"mt ([\d.]+) (\w) (.*)$", ln))]


@dataclass(frozen=True)
class BtState:
    status: int                 # enum bt_status
    sink: int                   # enum pcm_sink_ids
    irq: int                    # IRQ stack bytes used
    irq_size: int
    addr: str | None            # the device on link 0


def parse_bt_status(lines: Iterable[str]) -> BtState | None:
    st, addr = None, None
    for ln in lines:
        if m := re.search(r"status (\d+) sink (\d+), irq stack used (\d+) of (\d+)", ln):
            st = (int(m[1]), int(m[2]), int(m[3]), int(m[4]))
        if m := re.search(r"bt connected 0: ([0-9A-F:]{17})", ln):
            addr = m[1]
    return BtState(*st, addr) if st else None


def no_folder_skip(keys: Iterable[str]) -> list[str]:
    """Now Playing takes a skip tap followed within 1 s by a seek hold (R R+,
    L L+) as next/previous folder: a whole-card search (~7 s without a
    dircache) that health counts as a stall. 1.5 s goes before such a hold."""
    out: list[str] = []
    last = None
    for k in keys:
        if k in ("R+", "L+") and last in ("R", "L"):
            out.append("1500")
        out.append(k)
        if not k.isdigit():
            last = k
    return out


def mass_storage_up(vid: str, pid: str) -> bool:
    """Whether the USB device VID:PID (a dev build's shell IDs) is a mass
    storage device (class 08) instead"""
    for intf in Path("/sys/bus/usb/devices").glob("*:1.0"):
        with suppress(OSError):
            dev = intf.resolve().parent
            if ((dev / "idVendor").read_text().strip(), (dev / "idProduct").read_text().strip(),
                    (intf / "bInterfaceClass").read_text().strip()) == (vid, pid, "08"):
                return True
    return False


class DecodeHashes:
    """Decode hashes ("HASH FRAMES PATH" messages): a play of PATH from its
    start must hash as earlier plays over as many frames"""

    def __init__(self) -> None:
        self.first: dict[tuple[str, str], str] = {}
        self.plays: Counter[tuple[str, str]] = Counter()
        self.diffs = 0

    def add(self, text: str) -> str | None:
        h, frames, path = text.split(" ", 2)
        ref = self.first.setdefault((path, frames), h)
        self.plays[(path, frames)] += 1
        if ref == h:
            return None
        self.diffs += 1
        return f"decode hash {h} differs from {ref} ({path}, {frames} frames)"

    def line(self) -> str:
        return (f"Decode hashes: {sum(self.plays.values())} plays of {len(self.plays)} "
                f"(file, frames), {sum(n > 1 for n in self.plays.values())} played more "
                f"than once, {self.diffs} differences")


class VideoStats:
    """The Video Player's messages (apps/plugins/videoplayer/stats.c): at a
    play's end (`end=`), sound underruns or too many dropped pictures fail
    it, as does a fault of its second core"""

    def __init__(self) -> None:
        self.ends: list[dict[str, str]] = []

    def add(self, text: str) -> str | None:
        if text.startswith("cpu1 stopped"):     # its second core faulted (worker.c)
            return f"video: {text}"
        f = dict(re.findall(r"(\w+)=(\S+)", text))
        if "end" not in f:
            return None
        f["line"] = text.split(maxsplit=1)[0]
        self.ends.append(f)
        if f["line"] == "sound" and f["under"].split("/")[0] != "0":
            return f"video underruns {f['under']} (count/ms)"
        if f["line"] == "pics":
            due = int(f["shown"]) + int(f["drop"])
            if due >= 50 and int(f["drop"]) > VIDEO_DROP_MAX * due:
                return f"video dropped {f['drop']} of {due} pictures"
        return None

    def line(self) -> str | None:
        pics = [v for v in self.ends if v["line"] == "pics"]
        if not pics:
            return None
        sound = [v for v in self.ends if v["line"] == "sound"]
        due = sum(int(v["shown"]) + int(v["drop"]) for v in pics)
        idle = [int(v["idle"]) for v in pics]
        text = (f"Video: {len(pics)} plays, {sum(int(v['drop']) for v in pics)} of {due} "
                f"pictures dropped, {sum(int(v['late'].split('/')[0]) for v in pics)} late "
                f"(worst {max(int(v['late'].split('/')[1]) for v in pics)} ms), idle "
                f"{min(idle)}..{max(idle)}%")
        if sound:
            text += (f"; sound underruns {sum(int(v['under'].split('/')[0]) for v in sound)}, "
                     f"slowest read {max(int(v['read'].split('/')[0]) for v in sound)} ms")
        return text


class Coverage:
    """The crawl's graph of the UI, kept across runs: nodes "PATH NAME" (every
    main menu node, and "wps Now Playing"), each with its visits, the
    activities seen there and its edges: a key pressed there, how often, and
    the activities it led to"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.nodes: dict[str, dict[str, Any]] = (json.loads(path.read_text())
                                                 if path.exists() else {})

    def tried(self, key: str) -> int:
        edges = self.nodes.get(key, {}).get("edges", {})
        return sum(edges.get(k, {}).get("n", 0) > 0 for k in CRAWL_KEYS)

    def node(self, key: str, kind: str) -> dict[str, Any]:
        return self.nodes.setdefault(key, {"kind": kind, "visits": 0, "acts": [], "edges": {}})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.nodes, indent=1, sort_keys=True))

    def _ordered(self) -> list[tuple[str, dict[str, Any]]]:
        def order(kv: tuple[str, dict[str, Any]]) -> list[int]:
            p = kv[0].split()[0]
            return [int(x) for x in p.split(".")] if p[0].isdigit() else [-1]
        return sorted(self.nodes.items(), key=order)

    def summary(self) -> list[str]:
        edges = sum(1 for c in self.nodes.values() for e in c.get("edges", {}).values() if e["n"])
        lines = [f"{len(self.nodes)} nodes visited, {edges} of {len(self.nodes) * len(CRAWL_KEYS)} "
                 f"(node, key) edges tried, {sum(c['visits'] for c in self.nodes.values())} visits"]
        for k, c in self._ordered():
            es = " ".join(f"{key}>{','.join(map(str, e['to']))}"
                          for key, e in sorted(c.get("edges", {}).items()))
            lines.append(f"  {c['kind']} {k}  x{c['visits']}  act {c['acts']}  {es}")
        return lines

    def dot(self) -> list[str]:
        def act(a: int) -> str:
            return ACT_NAMES[a] if 0 <= a < len(ACT_NAMES) else str(a)
        lines = ["digraph crawl {", "  rankdir=LR; node [shape=box, fontsize=9];"]
        for k, c in self._ordered():
            lines.append(f'  "{k}";')
            lines += [f'  "{k}" -> "{act(to)}" [label="{key}", fontsize=8];'
                      for key, e in c.get("edges", {}).items() for to in e["to"]]
        return [*lines, "}"]


def coverage_file(t: Target) -> Path:
    return t.conf.path("menu_coverage", str(t.ws.build_dir / f"menu-coverage-{t.name}.json"))


# ---------------------------------------------------------------- this PC as the headset

class PcLink:
    """This PC's Bluetooth adapter as the player's headset, through BlueZ
    (bluetoothctl, busctl). While routing, the player's stream is kept on a
    null sink (made if missing, removed again). TRANSPORT: the adapter for
    Bumble ("" if none)."""

    def __init__(self, player: str, sink: str, transport: str = "") -> None:
        self.player, self.sink, self.transport = player.upper(), sink, transport
        self.adapter = ""
        self.path = ""                  # the player's D-Bus object, for AVRCP
        self.power_state = ""
        self.last_connect = ""
        self._stop = threading.Event()
        self._router: threading.Thread | None = None
        self._module = ""

    @staticmethod
    def ctl(*args: str, timeout: float = 20) -> str:
        try:
            return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True,
                                  timeout=timeout, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def find(self) -> bool:
        """The adapter's address; whether the player is among its paired
        devices, with its D-Bus path"""
        try:
            tree = subprocess.run(["busctl", "--list", "tree", "org.bluez"], capture_output=True,
                                  text=True, timeout=20, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        m = re.search(r"Controller ([0-9A-F:]{17})", self.ctl("show"))
        if not m:
            return False
        self.adapter = m[1]
        if self.player not in self.ctl("devices", "Paired"):
            return False
        dev = "dev_" + self.player.replace(":", "_")
        self.path = next((p for p in tree.split() if p.endswith(dev)), "")
        return bool(self.path)

    def powered(self) -> bool:
        return "Powered: yes" in self.ctl("show")

    def power(self, on: bool, secs: float = 10) -> bool:
        """Whether the adapter got there within SECS (BlueZ's PowerState, or
        Powered without it)"""
        want = "on" if on else "off"
        self.ctl("power", want)
        end = time.monotonic() + secs
        while True:
            show = self.ctl("show")
            m = re.search(r"PowerState: (\S+)", show)
            self.power_state = m[1] if m else ("on" if "Powered: yes" in show else "off")
            if self.power_state == want or time.monotonic() >= end:
                return self.power_state == want
            time.sleep(0.5)

    def others(self) -> list[str]:
        """Devices but the player connected to the adapter"""
        return [a for a in re.findall(r"Device ([0-9A-F:]{17})", self.ctl("devices", "Connected"))
                if a != self.player]

    def connected(self) -> bool:
        return bool(bluez.connected(self.player))

    def disconnect(self) -> None:
        self.ctl("disconnect", self.player)

    def remove(self) -> None:
        """BlueZ forgets the player (its link key, its trust)"""
        self.ctl("remove", self.player)

    @contextmanager
    def agent(self, log: Path, discoverable: bool) -> Generator[None]:
        """A bluetoothctl agent answering BlueZ's pairing prompts with yes, its
        lines in LOG. DISCOVERABLE: after remove() BlueZ no longer knows the
        player, and the adapter takes its page only while discoverable (page
        scan follows the paired devices)."""
        with (log.open("a", buffering=1) as f,
              pcsink.Agent(PLAYER_NAME, "NoInputNoOutput", f, discoverable=discoverable,
                           say=lambda text: f.write(text + "\n"))):
            yield

    def responsive(self) -> bool:
        """Whether the controller completes an HCI command (scan enable)"""
        ok = "succeeded" in self.ctl("discoverable", "on")
        self.ctl("discoverable", "off")
        return ok

    def reclaim(self, secs: float = 30) -> bool:
        """The adapter back with BlueZ after a Bumble peer had it: whether BlueZ
        has it, powered and answering within SECS"""
        end = time.monotonic() + secs
        attached = reset = False
        while time.monotonic() < end:
            if "Controller" in self.ctl("show"):
                if (self.powered() or self.power(True)) and self.responsive():
                    return True
                if not reset:
                    # wedged (2026-10-31: every command Busy, PowerState
                    # stuck in on-disabling)
                    bluez.give_back(self.transport, reset=True)
                    reset = True
                    time.sleep(6)
                    continue
            elif not attached and time.monotonic() > end - secs + 5:
                bluez.give_back(self.transport)     # still detached from btusb
                attached = True
            time.sleep(1)
        return False

    def usb_reset(self) -> bool:
        """A USB reset of the adapter, then BlueZ has it again: its links had
        come up with no data on them (LMP ran; the player's L2CAP never
        arrived), which BlueZ's and Bumble's HCI_Resets left as it was (TS17
        torture 9, 2026-11-05, after a bt-headset step)"""
        bluez.give_back(self.transport, reset=True)
        time.sleep(6)
        return self.reclaim()

    def connect(self) -> None:
        self.last_connect = self.ctl("connect", self.player, timeout=40)

    def avrcp(self, method: str) -> None:
        with suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(["busctl", "call", "org.bluez", self.path, "org.bluez.MediaControl1",
                            method], capture_output=True, timeout=10, check=False)

    def start_routing(self) -> None:
        try:
            if bluez.sink_index(self.sink) is None:
                self._module = subprocess.run(
                    ["pactl", "load-module", "module-null-sink", f"sink_name={self.sink}"],
                    capture_output=True, text=True, timeout=10, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return
        self._router = threading.Thread(target=self._route, daemon=True)
        self._router.start()

    def _route(self) -> None:
        while not self._stop.wait(1.0):     # a new stream plays on the default sink till then
            bluez.route(self.player, self.sink)

    def stop_routing(self) -> None:
        self._stop.set()
        if self._router:
            self._router.join(timeout=30)
        if self._module:
            with suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["pactl", "unload-module", self._module], capture_output=True,
                               timeout=10, check=False)
            self._module = ""


# ---------------------------------------------------------------- the suite

class RockboxTorture(TortureSuite):
    """The torture run of a Rockbox dev build through the shared dev shell.
    Config ([targets.NAME]): bt_address, the player's BD_ADDR for the bt-*
    scenarios (with [bluetooth] sink and adapter); menu_coverage, the
    crawl's graph (default <paths.build>/menu-coverage-NAME.json)."""

    card_dir: ClassVar[str]         # the build's DEV_CARD_DIR
    main_menu: ClassVar[tuple[str, ...]] = MAIN_MENU
    slow_codecs: ClassVar[tuple[str, ...]] = ()
    expected_msgs: ClassVar[Mapping[str, re.Pattern[str]]] = {}
    charge_limit: ClassVar[int | None] = None
    health_words: ClassVar[tuple[str, ...]] = ("plugin",)
    line_max: ClassVar[int] = SHELL_LINE

    def __init__(self, target: Target, image: Path, **kw: Any) -> None:
        super().__init__(target, image, **kw)
        self.msg_next = 1               # the next `msgs` number (from 1 after a boot)
        self.pending: list[tuple[str, str]] = []    # messages a scenario read early
        self.step_expected: list[re.Pattern[str]] = []  # and the ones this step may show
        self.messages: Counter[tuple[str, str]] = Counter()
        self.msgs_lost = 0
        self.decodes = DecodeHashes()
        self.videos = VideoStats()
        self.linein_db: list[float] = []
        self.battery: list[tuple[float, int, int, bool]] = []
        self.tree: list[MenuNode] | None = None
        self.sweep: list[str] | None = None     # pluginsweep's plugins still to run
        self.cfg_saved = False
        self.coverage = Coverage(coverage_file(target))
        conf, bt = target.conf, target.ws.section("bluetooth")
        address = conf.string("bt_address") if "bt_address" in conf.data else None
        self.pc = (PcLink(address, bt.string("sink", "arb_null"), bt.string("adapter", ""))
                   if address else None)
        have = {ROCKBOX}
        if self.pc:
            have |= {BT_PC} | ({BT_BUMBLE} if self.pc.transport and have_bumble() else set())
        self.features = self.features | have
        self.bt_known = False
        self.bt_off = ""                # why the bt-* scenarios do nothing this run
        self.bt_recovering = False
        self.bt_lost = 0                # links a bt-* case let vanish on purpose
        self.warming = False            # prepare()'s screen visit (its Home may boot again)

    # ---- the card's dev folder

    def card_path(self, name: str) -> str:
        return f"{self.card_dir}/{name}"

    @property
    def tone(self) -> str:
        """-15 dBFS for 10 s, then -27 dBFS"""
        return self.card_path("tone.mp3")

    @property
    def theme_cfgs(self) -> list[str]:
        return [*THEMES, self.card_path("t_default.cfg")]

    def cfgs(self) -> dict[str, str]:
        """The settings files the steps load, by name in card_dir"""
        files = dict(CFGS)
        if self.charge_limit is not None:
            files["t_limit.cfg"] = f"charge limit: {self.charge_limit}\n"
        return files

    # ---- the suite's hooks

    def start(self, out: Path) -> None:
        if self.pc:         # the player streams to this PC from its boot on
            self.pc.start_routing()
        if self.do_boot:    # before the run takes the device lock: mass storage takes it
            self.put_card_files()
        super().start(out)

    def release(self) -> None:
        if self.pc:
            self.pc.power(True)         # a bt-pcpower step may have died
            self.pc.stop_routing()

    def boot_settings(self) -> dict[str, str]:
        """The run settings of every boot (apps/dev_shell.c, the target's)"""
        return {}

    def boot_request(self) -> RunRequest:
        return RunRequest(self.image, 0, settings=self.boot_settings(), usb=True)

    def put_card_files(self) -> None:
        """The settings files into card_dir before the run's first boot, with
        the player booted from the run's image as USB mass storage; a player
        that puts them there at every boot overrides this"""
        if not self.target.has("storage"):
            raise TortureError(f"{self.target.name}: no USB mass storage to put the settings "
                               f"files into {self.card_dir} on its card")
        self.to_host()
        with self.target.mass_storage(CARD_S, self.image) as dev, card.mounted(dev) as mp:
            folder = mp / self.card_dir.lstrip("/")
            folder.mkdir(parents=True, exist_ok=True)
            for name, text in self.cfgs().items():
                (folder / name).write_text(text)
        print(f"settings files in {self.card_dir} on the card")

    def prepare(self) -> None:
        self.msg_next, self.tree = 1, None
        self.sh("dryoff")               # a menu power-off returns to the host
        self.sh("repeat 0")
        self.sh(f"vol {VOLUME_DB}")
        if self.charge_limit is not None:
            self.sh(f"cfg {self.card_path('t_limit.cfg')}")
        # the run's light look from the boot on: crawl restores the settings
        # it saved at its first step, and a YP3's default cabbiev2 then came
        # back after every crawl step, refusing Vorbis and Opus and taking
        # 35 KB of free memory
        self.sh(f"cfg {self.card_path('t_default.cfg')}")
        super().prepare()
        # the boot-time database update stops playback and its commit takes
        # all free RAM: steps start after it
        for _ in range(300):
            if not self.health().num("db"):
                break
            time.sleep(2)
        # the recording screen loads the theme's skin for it at its first
        # visit and keeps it, like every skin (~68 KB on the TS17): a visit
        # now puts it in the free memory reference, not in a later step
        if not self.warming and "recording" in self.main_menu:
            self.warming = True
            try:
                if self.menu("recording") == ACT_RECORDING:
                    self.home()
            finally:
                self.warming = False

    def expected_return(self, records: Records) -> str | None:
        if "power_off() (dry run" in dict(records).get("dev log (tail)", ""):
            return "SHUTDOWN (menu power-off, expected)"
        return None

    def preflight(self) -> None:
        """The card's SD codecs from another build fail every load, for hours
        (module build ID, docs/testing.md): stop before the first step"""
        play = f"play {PROBE}"
        self.sh(play)
        time.sleep(3)
        log = self.sh("log")
        idx = [i for i, ln in enumerate(log) if play in ln]
        errs = [ln for ln in log[idx[-1] if idx else -40:] if "codec load" in ln]
        self.sh("stop")
        if errs:
            raise TortureError(f'"{errs[0].strip()}": the card\'s codecs are not from this '
                               "build; install its modules (docs/testing.md)")
        self.read_msgs()        # those from before the run are not its own

    def after_step(self) -> list[str]:
        return self.take_messages() + self.charge_check() + self.sink_check()

    def sink_check(self) -> list[str]:
        """The output on Bluetooth with no device linked: a scenario forced
        the sink (`bt stack sink bt`) and its device never came or left, and
        every later play would sit at 0 (TS17 torture 9 steps 34-35, after
        bt-headset). The output goes back to the built-in one."""
        if not self.pc:
            return []
        for wait in (0, BT_SETTLE_S):       # a link loss's routing may be under way
            time.sleep(wait)
            bt = parse_bt_status(self.sh("bt stack status"))
            if bt is None or bt.sink != SINK_BT or bt.addr is not None:
                return []
        self.sh("bt stack sink builtin")
        return ["output left on Bluetooth with no device linked (set back to the built-in one)"]

    def busy(self, h: Health) -> str | None:
        return "a database build" if h.num("db") else None

    def settled(self, h: Health) -> bool:
        return (h.num("audio") == 0 and h.text("plugin") == "-" and h.num("db") == 0
                and h.num("act") == ACT_MAINMENU)

    def report_lines(self) -> list[str]:
        lines = ["", self.decodes.line()]
        if video := self.videos.line():
            lines.append(video)
        if self.linein_db:
            lines.append(f"Line In tone: {min(self.linein_db):.1f}..{max(self.linein_db):.1f} "
                         f"dBFS over {len(self.linein_db)} steps")
        if self.battery:
            lines.append("Battery (mV, %, charging): " + ", ".join(
                f"{time.strftime('%H:%M', time.localtime(t))} {mv} {lv} {int(c)}"
                for t, mv, lv, c in self.battery[-12:]))
        if self.messages:
            lines += ["", f"Messages lost unread: {self.msgs_lost}", "",
                      "| messages seen | times |", "|---|---|"]
            lines += [f"| {k} {t} | {n} |" for (k, t), n in self.messages.most_common()]
        return lines

    def wrap_up(self) -> None:
        self.sh("stop")
        self.sh(f"cfg {self.card_path('t_default.cfg')}")     # the default look and timeouts
        self.sh(f"cfg {self.card_path('t_wake.cfg')}")

    # ---- what a player's build may have (its features)

    def gate_on(self) -> bool:
        """Whether the LCD controller's clock runs (off while the panel
        sleeps): players that declare LCD_GATE read it"""
        raise TortureError(f"{self.target.name}: no LCD clock gate read")

    def fm_tune(self, khz: int | None) -> None:
        """The tuner on at KHZ (None: off) without the radio screen, for a
        recording of the FM input: players that declare FM_TUNE (the shared
        shell tunes only through the screen, `radio`)"""
        raise TortureError(f"{self.target.name}: no tuning without the radio screen")

    def bt_link_table(self) -> list[str]:
        """The BD_ADDRs in use in the controller's link table, where the
        player can read it (a refused page leaves its entry stale)"""
        return []

    def bt_fail_details(self) -> list[str]:
        """The player's own traces of a failed connect, for bt_fail_log"""
        return []

    # ---- what every step is checked for

    def read_msgs(self) -> list[tuple[str, str]]:
        """The messages since the last read"""
        nxt, msgs = parse_msgs(self.sh(f"msgs {self.msg_next - 1}"))
        if nxt is not None and nxt < self.msg_next:     # numbered from 1 again: a reboot
            self.msg_next = 1
            nxt, msgs = parse_msgs(self.sh("msgs 0"))
        if nxt is None:
            return []                   # a build without `msgs`
        self.msg_next = nxt
        return msgs

    def msgs(self) -> list[tuple[str, str]]:
        """The step's messages so far, for a scenario (after_step still judges them)"""
        self.pending += self.read_msgs()
        return self.pending

    def take_messages(self) -> list[str]:
        msgs, self.pending = self.pending + self.read_msgs(), []
        bad: list[str] = []
        for kind, text in msgs:
            if kind == "decode":
                bad += filter(None, [self.decodes.add(text)])
            elif kind == "video":
                bad += filter(None, [self.videos.add(text)])
            elif kind == "lost":
                self.msgs_lost += int(text)
            else:
                self.messages[(kind, text)] += 1
        bad += message_problems(msgs, self.expected_msgs.get(self.step.name),
                                SHARED_EXPECTED_MSGS.get(self.step.name), *self.step_expected)
        self.step_expected = []
        return [f"messages: {'; '.join(bad)[:300]}"] if bad else []

    def charge_check(self) -> list[str]:
        """A battery reading every ~10 min for the report; with a charge
        limit, the charger stays off above it"""
        b = parse_battery(self.sh("bat"))
        if b is None:
            return []
        mv, level, charging = b
        if not self.battery or time.time() - self.battery[-1][0] > 600:
            self.battery.append((time.time(), mv, level, charging))
        if self.charge_limit is not None and charging and level > self.charge_limit:
            return [f"charging at {level}% ({mv} mV), limit {self.charge_limit}%"]
        return []

    # ---- playback

    def playing(self) -> Status:
        return parse_status(self.sh("status"))

    def expect_playing(self, path: str | None = None, marker: str | None = None) -> bool:
        """Fails the step unless playback progresses (playing, 1.5 s played
        over 3 s), on PATH if given (not skipped), with no codec error
        logged after the line holding MARKER"""
        s1 = self.playing()
        time.sleep(3)
        s2 = self.playing()
        bad = []
        if s2.audio != AUDIO_PLAYING or progress_ms(s1, s2) < 1500:
            bad.append(f"not progressing (audio {s1.audio}->{s2.audio}, elapsed "
                       f"{s1.elapsed}->{s2.elapsed})")
        if path and s2.path != path:
            bad.append(f"playing {s2.path}, not {path}")
        log = self.sh("log")
        if marker:
            idx = [i for i, ln in enumerate(log) if marker in ln]
            log = log[idx[-1]:] if idx else log[-40:]
        if errs := [ln for ln in log if CODEC_ERRORS.search(ln)]:
            bad.append("codec error: " + errs[0][:100])
        if bad:
            self.fail("; ".join(bad))
        return not bad

    def codec_files(self) -> list[str]:
        return [f for f in ls_entries(self.sh(f"ls {CODECTEST}"))
                if CODEC_EXT.search(f) and not f.startswith(self.slow_codecs)]

    def pick_codec_file(self) -> str | None:
        files = self.codec_files()
        if not files:
            self.fail(f"no test files in {CODECTEST}")
            return None
        return self.rng.choice(files)

    def wait_within(self, wait: float) -> float:
        """WAIT, or less so a playing check (3 s) ends before the track does
        (some test files are 12 s): what is left of it, not its length (a
        status read ~1 s in left a 20 s file 0.2 s short)"""
        s = self.playing()
        return max(1, min(wait, (s.length - s.elapsed) / 1000 - 5)) if s.length else wait

    def library_track(self, album: bool = False) -> str | None:
        """A random track of the music library (artist/album folders, the
        stress set too): a random walk LIBRARY_DEPTH folders deep that backs
        out of folders without one (a stock card's Picture/, Video/, ...),
        in at most LIBRARY_LS listings; ALBUM: never one of the songs at the
        card's root"""
        not_library = ("codectest", self.card_dir.strip("/"))   # and torture*: the fs scenario's
        listings = LIBRARY_LS

        def walk(path: str, depth: int) -> str | None:
            nonlocal listings
            listings -= 1
            entries = ls_entries(self.sh(f"ls {path or '/'}"))
            songs = [e for e in entries if re.search(r"\.(mp3|flac)$", e, re.I)]
            if songs and (path or not album):
                return f"{path}/{self.rng.choice(songs)}"
            dirs = [e.rstrip("/") for e in entries if e.endswith("/") and not e.startswith(".")
                    and e.rstrip("/") not in not_library and not e.startswith("torture")]
            self.rng.shuffle(dirs)
            for d in dirs if depth < LIBRARY_DEPTH else ():
                if listings == 0:
                    break
                if track := walk(f"{path}/{d}", depth + 1):
                    return track
            return None

        return walk("", 1)

    def music(self) -> None:
        """A library track playing under a UI scenario, so health catches the
        hitches the UI causes. Not under a plugin the last step's keys left
        open: one holding the audio buffer (an image the monkey opened) made
        the shell's play panic (audio_reset_buffer(): OOM!)."""
        self.ready_to_play()
        if self.health().num("audio") != AUDIO_PLAYING and (track := self.library_track()):
            self.sh(f"play {track}")
            time.sleep(2)

    # ---- screens and keys

    def in_plugin(self) -> bool:
        """A plugin runs (one `run` started, or one opened from the menus):
        it holds the module arena, so codec loads fail meanwhile"""
        h = self.health()
        return h.text("plugin") != "-" or h.num("act") == ACT_PLUGIN

    def plugin_keys(self, tokens: list[str]) -> bool:
        """Keys for a plugin, one at a time while it runs: once it quits (a
        Back, an exit chord, a dismissed "Unsupported format" screen) the
        rest would work the menus instead (Up, Select: the empty Shortcuts
        menu's "No files" splash). False once it has quit."""
        for k in tokens:
            if not self.in_plugin():
                return False
            self.keys(k)
        return True

    def close_plugin(self) -> bool:
        """Out of any running plugin; False if no exit worked. The exits
        twice: one can move the plugin to a screen an earlier one leaves
        (random keys left an Arduboy game in its Settings, where the pause
        exits only picked options; a later exit's Select resumed the game,
        which the pause exit then left: TS17 sweep, amazeab, ardubloxx)"""
        for script in PLUGIN_EXITS * 2:
            if not self.plugin_keys(script.split()):
                return True
        return not self.in_plugin()

    def ready_to_play(self) -> None:
        """Before a playback check: a plugin left open, or the recording or
        radio screen (they hold the audio: a play does nothing), would fail
        it for the wrong reason. Boot again if a plugin will not close (one
        the random keys opened from the menus, that no exit leaves: a keymap
        bug); Home leaves those screens (random keys reach them, and Left
        there is the gain)."""
        if not self.close_plugin():
            h = self.health()
            self.fail(f"plugin {h.text('plugin')} (activity {h.num('act')}) did not exit: "
                      f"booted again ({self.screen('plugin')})")
            self.boot()
        elif self.health().num("act") in (ACT_RECORDING, ACT_FM):
            self.home()

    def ui_idle(self, limit: float = 90) -> None:
        """Waits while the UI thread misses its 10 s ping (e.g. a free-space
        rescan, ~35 s on a 32 GB card): keys meanwhile are lost"""
        end = time.monotonic() + limit
        while time.monotonic() < end and self.health().num("ui_age") > 10:
            time.sleep(1)

    def home(self) -> bool:
        """Home (Up+Left) twice: the main menu, with its first item selected
        (dev builds: HAVE_HOME_SELECTS_FIRST_ITEM). Screens that ignore Home
        get Back presses and plugin exits first; if nothing works, boot again
        and return False."""
        for first in HOME_FIRST_KEYS:
            self.ui_idle()
            self.keys(first + "UL 500 UL 500")
            if self.health().num("act") == ACT_MAINMENU:
                return True
            time.sleep(5)               # a busy UI may not have missed a ping yet
        self.boot()
        return False

    def menu(self, item: str) -> int | None:
        """A main menu item opened with real keys: the activity after"""
        if not self.home():
            return None
        index = self.main_menu.index(item)
        self.keys("D 150 " * index)
        self.select_named(str(index))
        self.keys("S 2500")
        act = self.health().num("act")
        if item == "database" and act == ACT_MAINMENU:
            self.keys("S 2500")         # out of heap while playing: "Stop playback?" yes
            act = self.health().num("act")
        return act

    def walk_to(self, path: str, last_wait: int) -> None:
        """Down and Select down a `menutree` PATH from the main menu, each
        Select on the item the path names (select_named)"""
        steps = path.split(".")
        for i, k in enumerate(steps):
            self.keys("D 150 " * int(k))
            self.select_named(".".join(steps[:i + 1]))
            self.keys(f"S {last_wait if i == len(steps) - 1 else 1500}")

    def select_named(self, path: str) -> None:
        """The item at menu tree PATH selected (its last level's index), and
        it must read as the tree names it. The tree leaves out what a menu
        callback hides from the shell's context (File View shows only once
        Settings is the activity), so a list can hold more items than it
        has: then Down through the list until the name is selected. A name
        not in the list (a stale tree: an item whose text changed) gets a
        note and the index."""
        index = int(path.rpartition(".")[2])
        name = next((n.name for n in self.menu_tree() if n.path == path), "")[:SEL_ITEM_MAX]
        s = self.select_at(index)
        if s is None or not name or s.item == name:
            return
        for _ in range(s.count + SEL_TRIES):     # a dropped press: another lap
            self.keys("D 150")
            if (at := self.selection()) is None or at.item == name:
                return
        self.note(f"menu walk: {name!r} (menu tree {path}) is not in the list: by index")
        self.select_at(index)

    def select_at(self, index: int) -> Sel | None:
        """Down or Up until the list on screen has item INDEX selected: the
        UI loses a press while it has not taken the last key
        (button_queue_try_post() posts one only into an empty queue), and a
        busy UI is that slow. A build without `sel` is trusted blind. The
        list as last read."""
        s = None
        for _ in range(SEL_TRIES):
            s = self.selection()
            if s is None or s.index == index:
                return s
            self.keys(("D 150 " if s.index < index else "U 150 ") * abs(index - s.index))
        return s

    def selection(self) -> Sel | None:
        """The list on screen once `sel` stops changing (the UI has drawn the
        keys it took); None on a build without `sel`"""
        s = parse_sel(self.sh("sel"))
        for _ in range(SEL_READS):
            if s is None:
                return None
            time.sleep(SEL_SETTLE_S)
            again = parse_sel(self.sh("sel"))
            if again == s:
                break
            s = again
        return s

    def screen(self, what: str) -> str:
        """The screen kept beside the step: its file's name, or why not"""
        png = self.step_file(what, ".png")
        try:
            im, _ = screenshot(lambda line: self.reply(line, 60), self.target.lcd)
        except ArbError as e:
            return f"no screenshot: {e}"
        fb.scaled(im).save(png)
        return png.name

    def menu_fail(self, problem: str) -> None:
        """A main menu entry that did not open"""
        self.fail(f"{problem} ({self.screen('menu')})")

    def menu_tree(self) -> list[MenuNode]:
        """The dev `menutree` dump, once per boot"""
        if self.tree is None:
            self.tree = parse_menutree(self.sh("menutree", timeout=60))
        return self.tree

    def usbcycle(self) -> None:
        """A simulated unplug and replug: the shell must come back"""
        with suppress(Lost):
            self.reply("usbcycle", 10)          # the reply is usually lost
        time.sleep(15)
        for _ in range(30):
            if self.shell_up():
                with suppress(Lost):
                    self.reply("uptime", 10)
                    return
            time.sleep(3)
        raise Lost("shell did not come back after usbcycle"
                   + (" (Rockbox is in USB mass storage mode)"
                      if mass_storage_up(self.shell.vid, self.shell.pid) else ""))

    # ---- Bluetooth: this PC as the headset

    def bt_status(self) -> BtState:
        s = parse_bt_status(self.sh("bt stack status"))
        if s is None:
            raise Lost("no `bt stack status` line")
        return s

    def bt_up(self, s: BtState) -> bool:
        return self.pc is not None and s.status >= BT_CONNECTED and s.addr == self.pc.adapter

    def bt_wait(self, cond: Callable[[BtState], bool], secs: float) -> tuple[BtState, float | None]:
        """Polls until COND: the state, and the seconds it took (None: never)"""
        t0 = time.monotonic()
        while True:
            s = self.bt_status()
            if cond(s):
                return s, time.monotonic() - t0
            if time.monotonic() - t0 >= secs:
                return s, None
            time.sleep(1)

    def link(self) -> PcLink:
        if self.pc is None:
            raise TortureError(f"no [targets.{self.target.name}] bt_address configured")
        return self.pc

    def bt_ready(self) -> bool:
        """This PC paired in BlueZ, the player's radio on and connected to it"""
        pc = self.link()
        if self.bt_off:
            return False
        if not self.bt_known:
            # a run cut short in a Bumble step leaves the adapter detached
            # from btusb and the player unknown to BlueZ: reclaim() takes
            # care of the first, the pairing below of the second
            if not pc.reclaim():
                self.bt_off = "BlueZ has no adapter (bluetoothctl show)"
                return False
            pc.find()
            if not any("bt stack: status" in ln for ln in self.sh("bt stack status")):
                self.bt_off = "no Bluetooth in this build"
                return False
            self.bt_known = True
        if not pc.powered():
            if not pc.power(True):
                self.pc_stuck("setup")
                return False
            time.sleep(2)
        if self.bt_status().status == BT_OFF:
            self.bt_toggle()
        s = self.bt_status()
        if self.bt_up(s):
            return True
        if s.status == BT_OFF:
            self.fail("bt setup: power on from the menu failed")
            return False
        # not paired: the crawl reaches Forget, bt-headset and bt-hid forget
        # this PC on both sides
        paired = self.bt_paired()
        # the player kept this PC's key but BlueZ forgot the player (a run cut
        # short in a Bumble step): no connect gets through, straight to both
        # keys gone
        stale = paired and not pc.find()
        with self.pairing(not paired and not stale):
            for by_pc in () if stale else (False, True):
                # the player's link or page to another device (auto-connect
                # takes the most recent one), or its own page of the PC,
                # would keep the next try from getting through
                if self.bt_status().addr and (by_pc or s.addr != pc.adapter):
                    self.sh("bt stack disconnect")
                    self.bt_wait(lambda st: st.addr is None, 8)     # page timeout 5.12 s
                if self.bt_reconnect("setup", by_pc, quiet=True) is not None:
                    if by_pc:
                        self.note(f"the player's connect failed (linked to "
                                  f"{s.addr or 'nothing'}); the PC's worked")
                    return True
        # the player forgot this PC while BlueZ kept its key, or the player's
        # key is another device's with this address (a Bumble peer's, if its
        # step was cut short): both keys go
        if paired:
            self.sh(f"bt stack forget {pc.adapter}")
        pc.remove()
        if self.bt_status().addr:       # BlueZ paged with its old key: unpaired, idle
            self.sh("bt stack disconnect")
            self.bt_wait(lambda st: st.addr is None, 8)
        with self.pairing(True):
            if self.bt_reconnect("setup", quiet=True) is not None:
                self.note("paired again only after removing "
                          + ("both keys" if paired else "BlueZ's old key"))
                return True
        # a page the controller refused right after a lost link (0405 0c)
        # keeps its link entry: later pages to that address fail 0b and the
        # peer's are detached (0x22) until Bluetooth goes off and on
        refused = any("command 0405 failed: 0b" in ln for ln in self.sh("log"))
        self.bt_fail_log("setup")
        s = self.bt_status()
        stale = self.bt_link_table() if refused and s.addr is None else []
        if stale and not self.bt_recovering:
            self.fail(f"bt setup: pages refused (0405 0b), link table holds "
                      f"{', '.join(stale)} with no link: Bluetooth off and on")
            self.bt_recovering = True
            try:
                if self.bt_toggle() and self.bt_toggle():
                    return self.bt_ready()
            finally:
                self.bt_recovering = False
            return False
        if not self.bt_recovering:
            # this PC's adapter may be the one at fault: a USB reset, once
            self.bt_recovering = True
            try:
                if not pc.usb_reset():
                    self.fail("bt setup: BlueZ did not get this PC's adapter back after a USB "
                              "reset: bt off for this run")
                    self.bt_off = "BlueZ lost this PC's adapter"
                    return False
                if self.bt_ready():         # a failure there is recorded there
                    self.note("this PC's adapter needed a USB reset (links came up with no "
                              "data); the player then connected")
                    return True
                return False
            finally:
                self.bt_recovering = False
        self.fail(f"bt setup: no connection from either side within {BT_CONNECT_S} s: status "
                  f"{s.status}, player linked to {s.addr or 'nothing'}, paired "
                  f"{self.bt_paired()}, PC sees {'connected' if pc.connected() else 'no link'}")
        return False

    def bt_paired(self) -> bool:
        """Whether the player has this PC among its paired devices"""
        return any(ln.startswith("bt paired") and self.link().adapter in ln
                   for ln in self.sh("bt stack list"))

    @contextmanager
    def pairing(self, on: bool, discoverable: bool = True) -> Generator[None]:
        """With ON, a pairing agent on this PC while the block runs (its lines
        beside the step); then the player trusted (BlueZ asks no agent
        before its profiles connect) and its D-Bus path found again"""
        if not on:
            yield
            return
        pc = self.link()
        with pc.agent(self.step_file("bt-agent", ".log"), discoverable):
            yield
        bluez.trust(pc.player)
        pc.find()

    def bt_toggle(self) -> bool:
        """Settings > Bluetooth > "Bluetooth: On/Off" with real keys (a walk
        from the main menu's first item)"""
        node = next((n for n in self.menu_tree() if re.match(r"Bluetooth: O(n|ff)$", n.name)),
                    None)
        if not node:
            self.fail("bt: no Bluetooth power item in the menu tree")
            return False
        if not self.home():
            return False
        self.walk_to(node.path, 2000)
        self.ui_idle(30)                # power-on blocks the UI (calibration: up to 6 s)
        self.home()
        return True

    def bt_fail_log(self, what: str) -> None:
        """A failed connect: the device log (with the player's connect traces),
        the player's own traces and what bluetoothctl said, beside the step"""
        out = [f"{time.strftime('%F %T')} {what}", "", "--- bluetoothctl connect",
               self.link().last_connect or "(none)", "--- sh log"]
        try:
            out += self.sh("log")
        except Lost as e:
            out.append(f"(no reply: {e})")
        out += self.bt_fail_details()
        self.step_file("bt-fail", ".txt").write_text("\n".join(out) + "\n")

    def bt_reconnect(self, what: str, by_pc: bool = False, quiet: bool = False) -> float | None:
        """A connect (the player pages, or the PC does): the seconds it took"""
        pc = self.link()
        t0 = time.monotonic()
        if by_pc:
            pc.connect()
        else:
            self.sh(f"bt stack connect {pc.adapter}")
        s, t = self.bt_wait(self.bt_up, max(1.0, BT_CONNECT_S - (time.monotonic() - t0)))
        if t is None:
            if not quiet:
                self.bt_fail_log(what)
                self.fail(f"{what}: no connection within {BT_CONNECT_S} s: status {s.status}, "
                          f"player linked to {s.addr or 'nothing'}")
            return None
        return time.monotonic() - t0

    def bt_gone(self, what: str, secs: float = 10) -> bool:
        """The link must drop; then playback pauses on the built-in output"""
        s, t = self.bt_wait(lambda st: st.status < BT_CONNECTED, secs)
        if t is None:
            self.fail(f"{what}: still connected after {secs:.0f} s: status {s.status}, "
                      f"player linked to {s.addr or 'nothing'}")
            return False
        self.bt_settled(what)
        if self.health().num("audio") == AUDIO_PLAYING:
            self.fail(f"{what}: still playing after the headset went away")
        return True

    def bt_settled(self, what: str, settle: bool = True) -> None:
        """The built-in output unless a stream is open, the setting as the
        radio, IRQ stack headroom, no device but this PC. A power-off from
        the menu routes and stores at once: no settling time then."""
        if settle:
            time.sleep(BT_SETTLE_S)
        s = self.bt_status()
        bad = []
        if s.status < BT_CONNECTED and s.sink != SINK_BUILTIN:
            bad.append(f"stuck PCM sink {s.sink} (status {s.status})")
        if s.status == BT_STREAMING and s.sink != SINK_BT:
            bad.append("streaming, but on the built-in output")
        on = next((m[1] == "on" for ln in self.sh("set bluetooth")
                   if (m := re.match(r"bluetooth: (on|off)$", ln))), None)
        if on is not None and on != (s.status != BT_OFF):
            bad.append(f"setting {'on' if on else 'off'}, radio status {s.status}")
        if s.irq * 10 > s.irq_size * 9:
            bad.append(f"IRQ stack {s.irq} of {s.irq_size} B used")
        if s.addr and s.addr != self.link().adapter:
            bad.append(f"connected to {s.addr}, not this PC")
        if bad:
            self.fail(f"{what}: {', '.join(bad)}")

    def bt_resume(self) -> None:
        """Play again (the policy pauses when a headset goes away), first
        leaving a recording or radio screen that browsing reached (torture
        17 step 75: the main menu kept its cursor on Recording)"""
        self.ready_to_play()
        audio = self.health().num("audio")
        if audio == AUDIO_PAUSED:
            self.sh("wps")
            time.sleep(1)
            self.keys("S 500")
        elif audio == 0 and (track := self.library_track()):
            self.sh(f"play {track}")
            time.sleep(2)

    def bt_streaming(self, what: str, secs: float = 6) -> bool:
        """Resumed: streaming to this PC, elapsed time moving with the clock
        (a stalled link or PCM sink stops the mixer pulling)"""
        self.bt_resume()
        s, t = self.bt_wait(lambda st: st.status == BT_STREAMING, 10)
        if t is None:
            self.fail(f"{what}: not streaming: status {s.status} sink {s.sink}")
            return False
        rate, audio = 0.0, 0
        for _ in range(2):                      # again if the track changed
            h0, t0 = self.health(), time.monotonic()
            time.sleep(secs)
            h1 = self.health()
            (e0, n0), (e1, n1) = h0.pair("elapsed"), h1.pair("elapsed")
            rate, audio = (e1 - e0) / 1000 / (time.monotonic() - t0), h1.num("audio")
            if n0 == n1 and e1 >= e0:
                break
        if rate < 0.8:
            self.fail(f"{what}: elapsed moved at {rate:.2f}x real time (audio {audio})")
            return False
        return True

    def bt_bitpool(self, what: str) -> None:
        """The bitpool climbs back (the log's "bt: bitpool N" at each step,
        "bt: media ... bp N" every 10 s)"""
        t0, bp = time.monotonic(), None
        while True:
            for ln in self.sh("log"):
                if m := re.search(r"bt: (?:bitpool|media \d+ pkts bp) (\d+)", ln):
                    bp = int(m[1])
            if bp is not None and bp >= BT_BITPOOL_OK:
                return
            if time.monotonic() - t0 >= BT_BITPOOL_S:
                break
            time.sleep(5)
        self.fail(f"{what}: bitpool {bp} after {BT_BITPOOL_S} s at the desk "
                  f"(want {BT_BITPOOL_OK})")

    def lend_adapter(self, what: str) -> bool:
        """Before a Bumble peer takes this PC's adapter: the link goes and both
        sides forget their keys (the peer has this PC's address but keys of
        its own; BlueZ pages the player as soon as it has the adapter back,
        and with its old key that link hangs unpaired)"""
        pc = self.link()
        self.sh("bt stack disconnect")
        if not self.bt_gone(what):
            return False
        self.sh(f"bt stack forget {pc.adapter}")
        pc.remove()
        return True

    def take_back_adapter(self, what: str) -> None:
        """After the peer: the player forgets its key, BlueZ gets the adapter
        back, the player scans, and it pairs with this PC again and streams"""
        pc = self.link()
        self.sh(f"bt stack forget {pc.adapter}")
        if not pc.reclaim():
            self.fail(f"{what}: BlueZ did not get this PC's adapter back: bt off for this run")
            self.bt_off = "BlueZ lost this PC's adapter"
            return
        self.bt_rescan()
        if self.bt_ready():
            self.bt_streaming(what)

    def bt_rescan(self) -> None:
        """A scan by the player with this PC discoverable, to its end. The
        peer had this PC's address, and a page takes the link's class from
        the scan list: as an input device's, the player opens no AVDTP and
        BlueZ drops the idle link (every setup after a bt-hid step failed)."""
        pc = self.link()
        pc.ctl("discoverable", "on")
        try:
            self.sh("bt stack scan")
            end = time.monotonic() + BT_SCAN_S
            while time.monotonic() < end:
                time.sleep(1)
                if any("scanning 0" in ln for ln in self.sh("bt stack list")):
                    return
        finally:
            pc.ctl("discoverable", "off")

    def pc_stuck(self, what: str) -> str:
        """This PC's adapter did not reach the power state asked for: not the
        player's fault, and nothing more to test with it"""
        self.fail(f"{what}: this PC's adapter is stuck in PowerState "
                  f"{self.link().power_state} (needs a bluetoothd restart): bt off for this run")
        self.bt_off = "this PC's adapter is stuck"
        return what

    def bt_case(self, case: Callable[[RockboxTorture], str]) -> str:
        """A bt-* step: ready, music, the case; then the output settled, the
        bitpool back, health. The music repeats all step long: the end of its
        folder would stop the stream that the checks measure."""
        if not self.bt_ready():
            return f"skipped: {self.bt_off}" if self.bt_off else "setup failed"
        self.sh("repeat 1")
        self.bt_lost = 0
        try:
            self.music()
            h = self.health()
            what = case(self)
            self.sh(f"vol {VOLUME_DB}")            # after AVRCP volume keys
            if self.bt_off:
                return what
            self.bt_settled(what)
            if self.bt_status().status == BT_STREAMING:
                self.bt_bitpool(what)
            # a link that vanishes stalls playback until its supervision timeout
            self.check_health(h, allow={"stalls": self.bt_lost})
            return what
        finally:
            self.sh("repeat 0")


def rockbox_scenario(name: str, *, weight: int, needs: Iterable[str] = ()) -> Register:
    """A scenario of every player's RockboxTorture, needing NEEDS too"""
    return scenario(name, weight=weight, needs={ROCKBOX, *needs})


# ---------------------------------------------------------------- playback scenarios

@rockbox_scenario("codecs", weight=12)
def codecs(t: RockboxTorture) -> str | None:
    """A /codectest file, mostly on Now Playing (the worst case): it plays on"""
    if not (f := t.pick_codec_file()):
        return None
    t.ready_to_play()
    h = t.health()
    t.sh(f"play {CODECTEST}/{f}")
    if t.rng.random() < 0.7:
        t.sh("wps")
    time.sleep(t.wait_within(t.rng.uniform(8, 18)))
    t.check_health(h)
    t.expect_playing(f"{CODECTEST}/{f}", f"play {CODECTEST}/{f}")
    return f


@rockbox_scenario("decode", weight=10)
def decode(t: RockboxTorture) -> str | None:
    """A /codectest file from its start while the UI, the SD card or nothing
    else works; after_step compares its decode hash with earlier plays"""
    if not (f := t.pick_codec_file()):
        return None
    t.ready_to_play()
    t.sh("stop")
    h = t.health()
    t.sh(f"play {CODECTEST}/{f}")
    hz = t.playing().hz or 44100
    load = t.rng.choice(["keys", "wps", "sd", "none"])
    if load == "keys":                  # browse and scroll, never open a file
        t.keys(" ".join(t.rng.choices(["U", "D", "L", "U+", "D+", "200"], k=20)))
    elif load == "wps":
        t.sh("wps")
    elif load == "sd":
        t.sh(f"sdtest {t.rng.choice([64, 128, 256])} 16 odd", timeout=600)
    # the hash comes after DECODE_FRAMES or at the end; the codec runs at most
    # a few seconds ahead of the output
    time.sleep(t.wait_within(DECODE_FRAMES / max(hz, 8000) + 2))
    t.check_health(h)
    return f"{f} ({load})"


@rockbox_scenario("linein", weight=4)
def linein_tone(t: RockboxTorture) -> str | None:
    """The wired output on this PC's Line In (`arb linein serve`): the tone
    file's two tones whole, at a steady level, that of earlier steps"""
    directory = linein.directory(t.target.ws)
    latest = directory / "latest.json"
    if not latest.exists() or time.time() - latest.stat().st_mtime > 5:
        return "skipped: no Line In capture running (arb linein serve)"
    t.ready_to_play()
    t.sh("stop")                        # a sink switch under a playing track underruns
    bt = parse_bt_status(t.sh("bt stack status"))
    if bt is not None and bt.status == BT_ON and t.pc and not t.bt_off:
        # after a boot (ready_to_play's): the auto-connect's audio link would
        # take the output mid-tone
        _, took = t.bt_wait(lambda s: s.status >= BT_CONNECTED, BT_CONNECT_S)
        if took is not None:
            time.sleep(BT_SETTLE_S)
            bt = parse_bt_status(t.sh("bt stack status"))
    to_bt = bt is not None and bt.sink == SINK_BT
    if to_bt:
        t.sh("bt stack sink builtin")
    t.sh(f"vol {VOLUME_DB}")            # skips, monkey and AVRCP move it
    h = t.health()
    since = datetime.now(UTC)
    t.sh(f"play {t.tone}")
    time.sleep(1.5)
    s0, t1 = t.playing(), time.monotonic()
    if s0.path != t.tone:
        t.sh("stop")
        if to_bt:
            t.sh("bt stack sink bt")
        t.fail(f"the tone did not start (audio {s0.audio}, playing {s0.path})")
        return None
    # its -15 dBFS part lasts 10 s; no shell traffic meanwhile (long replies
    # slip DAC samples)
    time.sleep(10.5)
    s1 = t.playing()
    advance = f"playback advanced {(s1.elapsed - s0.elapsed) / 1000:.1f} s in " \
              f"{time.monotonic() - t1:.1f} s"
    sink = parse_bt_status(t.sh("bt stack status"))
    until = datetime.now(UTC)
    time.sleep(linein.LAG_S + 1.5)      # the capture publishes each second after it
    tc = linein.tone_check(directory, since, until, need=5, span=8)
    t.sh("stop")
    if to_bt:
        t.sh("bt stack sink bt")
    bad = list(tc.problems)
    if sink and sink.sink != SINK_BUILTIN:
        bad.append(f"output moved off the built-in sink (sink {sink.sink})")
    note = tc.note
    if tc.tone:
        onset = (tc.tone[0].second.published - since
                 - timedelta(seconds=linein.LAG_S)).total_seconds()
        if onset > LINEIN_ONSET_S:
            bad.append(f"the tone started {onset:.0f} s after the play command")
    levels = [x.channels[0].db for x in tc.tone[1:8]]   # whole seconds of its first 10 s
    if levels:
        mid = sorted(levels)[len(levels) // 2]
        note += f" at {mid:.1f} dBFS"
        if max(levels) - min(levels) > LINEIN_STEADY_DB:
            bad.append(f"level moved {min(levels):.1f}..{max(levels):.1f} dBFS")
        if t.linein_db:
            ref = sorted(t.linein_db)[len(t.linein_db) // 2]
            if abs(mid - ref) > LINEIN_DB:
                bad.append(f"level {mid:.1f} dBFS, earlier steps {ref:.1f}")
        t.linein_db.append(mid)
    t.transcript += ["", "Line In:", tc.report()]
    if bad:
        t.fail(f"{advance}; " + "; ".join(bad[:4]))
    t.check_health(h)
    return note


@rockbox_scenario("video", weight=4)
def video(t: RockboxTorture) -> str | None:
    """Video Player on a random /video file, random keys, then out (its
    stats are judged with the messages)"""
    vids = [v for v in ls_entries(t.sh(f"ls {VIDEO_DIR}")) if v.lower().endswith(VIDEO_EXT)]
    if not vids:
        return f"skipped: no videos in {VIDEO_DIR}"
    v = t.rng.choice(vids)
    t.ready_to_play()
    # Its settings at their defaults: the keys follow the picture as "Turn
    # video" holds it, and the quit below knows the clockwise turn only
    # (torture 30 step 116: an earlier run's keys left it anticlockwise,
    # where Left is Up, and Right picked an option that kept the menu)
    t.sh(f"rm {VIDEO_CFG}")
    h = t.health()
    t.sh(f"run {VIDEO_PLAYER} {VIDEO_DIR}/{v}")
    time.sleep(t.rng.uniform(4, 20))
    t.plugin_keys(t.rng.choices(["U", "D", "L", "R", "S", "200", "1000"], k=10))
    time.sleep(2)
    # Quit is the menu's last item (Up wraps to it), but a landscape video
    # turns the keys with the picture: Up is Right then
    for up in ("U", "R"):
        if t.in_plugin():
            t.keys(f"S+ 300 {up} 300 S")
            time.sleep(1)
    # the screen those left, before the other exits' keys change it
    # (torture 18 step 39: was the menu open?)
    shot = t.screen("video") if t.in_plugin() else ""
    if not t.close_plugin():
        t.fail(f"did not exit ({shot})")
        t.boot()
        return v
    t.check_health(h)
    return v


@rockbox_scenario("codecmix", weight=8)
def codecmix(t: RockboxTorture) -> str | None:
    """New playlists started back to back across codec families (each start
    may move the PCM ring or the codec's block), sometimes with an
    unsupported WMA among them: the last one must play"""
    stress = [f"{STRESS}/{e}" for e in ls_entries(t.sh(f"ls {STRESS}"))
              if re.search(r"\.\w+$", e) and not e.startswith(".")]
    pool = stress + [f"{CODECTEST}/{f}" for f in t.codec_files()]
    if len(pool) < 3:
        t.fail(f"too few files in {STRESS} and {CODECTEST}")
        return None
    seq = t.rng.sample(pool, t.rng.randint(3, 5))
    if t.rng.random() < 0.3:            # never last: the last one must play
        seq.insert(t.rng.randrange(len(seq) - 1), UNSUPPORTED)
    t.ready_to_play()
    h = t.health()
    for f in seq[:-1]:
        t.sh(f"play {f}")
        time.sleep(t.rng.uniform(1, 5))
    t.sh(f"play {seq[-1]}")
    time.sleep(t.wait_within(t.rng.uniform(11, 15)))
    # each start underruns (codecs and longplay count those): this case is
    # about the last track playing at all
    t.check_health(h, ignore=("underruns",))
    t.expect_playing(seq[-1], f"play {seq[-1]}")
    return " ".join(PurePosixPath(f).name for f in seq)


@rockbox_scenario("shuffle", weight=8)
def shuffle(t: RockboxTorture) -> str:
    """The codec stress playlist shuffled, with skips and seeks across
    codecs: the last track must play"""
    t.ready_to_play()
    h = t.health()
    t.sh(f"playlist {STRESS_PLAYLIST} shuffle")
    t.sh("wps")
    moves = ["R", "R", "R", "L", "R+", "L+", "300", "1500", "4000"]
    script = " ".join(no_folder_skip(t.rng.choice(moves) for _ in range(t.rng.randint(6, 16))))
    t.keys(script)
    time.sleep(10)
    t.check_health(h, ignore=("underruns",))    # each skip starts a track
    t.expect_playing(None, f"playlist {STRESS_PLAYLIST}")
    return script


@rockbox_scenario("skips", weight=6)
def skips(t: RockboxTorture) -> str | None:
    """Random next, previous, seeks, volume and pause on Now Playing"""
    if not (track := t.library_track()):
        return "skipped: no music library"
    h = t.health()
    t.sh(f"play {track}")
    t.sh("wps")
    moves = ["R", "L", "R+", "L+", "U", "D", "S", "S", "500", "1500"]
    script = " ".join(no_folder_skip(t.rng.choice(moves) for _ in range(25)))
    t.keys(script)
    time.sleep(3)
    t.check_health(h)
    return script


@rockbox_scenario("folders", weight=4)
def folders(t: RockboxTorture) -> str | None:
    """Next/previous folder on Now Playing (a skip tap, then a seek hold):
    another folder's track plays within FOLDER_BUDGET_S of the hold's release.
    Playback stands still meanwhile: stalls are expected."""
    track = next(filter(None, (t.library_track(album=True) for _ in range(6))), None)
    if not track:
        return "skipped: no album track found"
    t.sh(f"play {track}")
    t.sh("wps")
    time.sleep(3)
    if t.playing().audio != AUDIO_PLAYING:
        return f"skipped: {track} did not start"
    h = t.health()
    done: list[str] = []
    what = ""
    for _ in range(t.rng.randint(1, 4)):
        key = t.rng.choice(["R", "L"])
        s0 = t.playing()
        folder = PurePosixPath(s0.path).parent
        # the hold's first repeat must come within 1 s of the tap (wps.c):
        # `keys` waits 200 ms after each token, so no pause between
        t.keys(f"{key} {key}+")
        t0 = time.monotonic()
        while True:
            s = t.playing()
            secs = time.monotonic() - t0
            moved = PurePosixPath(s.path).parent != folder
            if (s.audio == AUDIO_PLAYING and moved) or secs > FOLDER_WAIT_S:
                break
            time.sleep(0.2)
        done.append(f"{key} {secs:.1f} s")
        what = f"{' '.join(done)} from {folder}"
        if not moved:
            t.fail(f"{what}: still in the folder after {secs:.0f} s (audio {s.audio}, elapsed "
                   f"{s0.elapsed / 1000:.1f} -> {s.elapsed / 1000:.1f} s, {s.path})")
            return what
        if secs > FOLDER_BUDGET_S:
            t.fail(f"{what}: {secs:.1f} s to the next folder (budget {FOLDER_BUDGET_S} s)")
            return what
        time.sleep(t.rng.choice([0.5, 2, 5]))
    t.expect_playing()
    # each folder skip runs the PCM dry while the next folder's playlist is built
    t.check_health(h, ignore=("stalls", "underruns"))
    return what


@rockbox_scenario("longplay", weight=4)
def longplay(t: RockboxTorture) -> str | None:
    """A library track on Now Playing for 1-5 min, health every 30 s"""
    if not (track := t.library_track()):
        return "skipped: no music library"
    h = t.health()
    t.sh(f"play {track}")
    t.sh("wps")
    end = time.monotonic() + t.rng.uniform(60, 300)
    while time.monotonic() < end:
        time.sleep(30)
        h = t.check_health(h)
    return track


@rockbox_scenario("theme", weight=4)
def theme(t: RockboxTorture) -> str:
    """A theme switch (stops playback, as upstream), then play on Now Playing"""
    cfg = t.rng.choice(t.theme_cfgs)
    t.sh(f"cfg {cfg}")
    time.sleep(4)
    if track := t.library_track():
        h = t.health()
        t.sh(f"play {track}")
        t.sh("wps")
        time.sleep(t.rng.uniform(8, 15))
        t.keys("UL 500")
        t.check_health(h)
    t.sh(f"cfg {t.card_path('t_default.cfg')}")    # left on, cabbiev2 refuses Vorbis and Opus later
    return cfg


# ---------------------------------------------------------------- UI scenarios

@rockbox_scenario("monkey", weight=8)
def monkey(t: RockboxTorture) -> str:
    """60 random keys anywhere over music, then back out (they may reach a
    menu's power-off: `dryoff` makes it an expected return)"""
    t.music()
    h = t.health()
    script = " ".join(no_folder_skip(t.rng.choices(MONKEY_KEYS, MONKEY_WEIGHTS, k=60)))
    t.keys(script)
    t.keys("L L L L L")
    t.check_health(h)
    return script


def plugin_choices(t: RockboxTorture, cat: str) -> list[str]:
    """A plugin folder's .rock files and its subfolders ("NAME/"), less
    PLUGIN_SKIP and hidden folders (games/.picross/: a plugin's data)"""
    return [e for e in ls_entries(t.sh(f"ls {ROCKS}/{cat}"))
            if (e.endswith(".rock") and e not in PLUGIN_SKIP)
            or (e.endswith("/") and not e.startswith("."))]


@rockbox_scenario("plugins", weight=6)
def plugins(t: RockboxTorture) -> str | None:
    """A random plugin over music, keys, then out with the player's keys (no
    health check: plugins take the CPU)"""
    t.music()
    cat = t.rng.choice(PLUGIN_DIRS)
    choices = plugin_choices(t, cat)
    if not choices:
        return f"skipped: no plugins in {ROCKS}/{cat}"
    pick = t.rng.choice(choices)
    if pick.endswith("/"):
        # a port's collection (games/arduboy/: a .rock per game) is one
        # choice, then one of its games
        cat += "/" + pick.rstrip("/")
        rocks = [r for r in plugin_choices(t, cat) if not r.endswith("/")]
        if not rocks:
            return f"skipped: no plugins in {ROCKS}/{cat}"
        pick = t.rng.choice(rocks)
    return try_plugin(t, f"{cat}/{pick}")


@rockbox_scenario("pluginsweep", weight=0)
def pluginsweep(t: RockboxTorture) -> str | None:
    """Every plugin once, in order, one a step, then random ones as in
    `plugins` (off by default: --only pluginsweep): a plugin that hangs or
    crashes the player is that step's failure, its `run` in the crash
    file's commands"""
    if t.sweep is None:
        t.sweep = []
        for cat in PLUGIN_DIRS:
            for e in plugin_choices(t, cat):
                if not e.endswith("/"):
                    t.sweep.append(f"{cat}/{e}")
                    continue
                sub = f"{cat}/{e.rstrip('/')}"
                t.sweep += [f"{sub}/{r}" for r in plugin_choices(t, sub) if not r.endswith("/")]
        t.sweep.sort()
        t.note(f"{len(t.sweep)} plugins to sweep")
    if not t.sweep:
        return plugins(t)
    t.music()
    return try_plugin(t, t.sweep.pop(0))


def try_plugin(t: RockboxTorture, p: str) -> str:
    """P (under ROCKS) run, random keys (Select starts a game from its start
    menu), then out with the player's keys; a known refusal (its splash
    seen) is a note and a reboot"""
    if splash := KNOWN_PLUGIN_SPLASHES.get(p.rsplit("/", 1)[-1]):
        t.step_expected.append(re.compile(splash))
    t.sh(f"run {ROCKS}/{p}")
    time.sleep(3)
    t.plugin_keys(t.rng.choices(["U", "D", "L", "R", "S", "200"], k=20))
    if not t.close_plugin():
        why = KNOWN_EXIT_REFUSALS.get(p.rsplit("/", 1)[-1])
        if why and any(kind == "splash" and re.search(why, text) for kind, text in t.msgs()):
            t.note(f"{p} refused to quit, as upstream does (splash {why}); rebooted")
        else:
            t.fail(f"did not exit (no way out with the player's keys: a keymap bug; "
                   f"{t.screen('plugin')})")
        t.boot()
    return p


@rockbox_scenario("menus", weight=6)
def menus(t: RockboxTorture) -> str:
    """A random main menu entry by keys over music, a random walk, and Home
    must bring the main menu back (no screen that traps the keys)"""
    t.music()
    # not Plugins: a game started by random keys need not quit on Back
    item = t.rng.choice(["files", "database", "settings", "playlists", "system", "shortcuts"])
    h = t.health()
    act = t.menu(item)
    walk = " ".join(t.rng.choices(["D", "D", "U", "S", "L", "300"], k=12))
    t.keys(walk)
    if act is None or not t.home():
        t.fail(f"Home did not reach the main menu (act {act})")
    t.check_health(h)
    return f"{item}: {walk}"


@rockbox_scenario("dbdeep", weight=4)
def dbdeep(t: RockboxTorture) -> str | None:
    """Deep into the database, Home, Database again: the next visit must
    start at the root (a stale table once spun the UI until the watchdog)"""
    t.music()
    h = t.health()
    act = t.menu("database")
    if act != ACT_DATABASE:
        # low memory: the menus come from free heap only
        msgs = t.msgs()
        if any("the database" in text for kind, text in msgs if kind == "yesno"):
            t.home()
            return f"refused: not enough memory while music plays (free {h.num('free')})"
        # no buffer for a commit (no dircache or ramcache, playback holds the
        # heap): tagcache delays it to the next boot, upstream's design
        if ("splash", DB_COMMIT_DELAYED) in msgs:
            t.home()
            return f"refused: the database commit waits for a reboot (free {h.num('free')})"
        t.menu_fail(f"main menu -> Database gave activity {act}, "
                    f"free {t.health().num('free')}")
        t.home()
        return None
    depth = t.rng.randint(1, 3)
    t.keys("D 300 " * t.rng.randint(0, 3) + "S 2500 " * depth)
    if not t.home():
        t.fail(f"depth {depth}: Home did not leave the database")
        return None
    act = t.menu("database")
    t.keys("1500")
    if act != ACT_DATABASE or t.health().num("ui_age") > 10:
        t.fail(f"depth {depth}: second visit: activity {act}")
    t.home()
    t.check_health(h)
    return f"depth {depth}"


@rockbox_scenario("db", weight=4)
def db(t: RockboxTorture) -> str:
    """The database browsed while playing; sometimes a foreground update
    (stops playback and holds the UI until done)"""
    h = t.health()
    t.sh(f"play {PROBE}")
    t.keys("L L L L D S 1000 D S 1000 S 2000 L L L L")
    time.sleep(5)
    h = t.check_health(h)
    if t.rng.random() >= 0.2:
        return "browse during playback"
    t.sh("db update")
    time.sleep(5)
    for _ in range(60):
        if not t.health().num("db"):
            break
        time.sleep(5)
    else:
        t.fail("db update: still building after 5 min")
    t.check_health(h)
    return "browse during playback, db update"


@rockbox_scenario("crawl", weight=16)
def crawl(t: RockboxTorture) -> str | None:
    """One step of exploring the UI graph (Coverage): to the node whose keys
    are least tried, its least-tried key (sometimes one more), the activity
    it led to recorded, then Home must get back. Music plays throughout (FM
    and Recording stop it); settings the keys change are restored after."""
    nodes = [n for n in t.menu_tree() if not CRAWL_SKIP.search(n.name)]
    if not nodes:
        t.fail("no menu tree")
        return None
    nodes.append(MenuNode("wps", "W", "Now Playing"))
    cov = t.coverage
    least = min(cov.tried(f"{n.path} {n.name}") for n in nodes)
    n = t.rng.choice([n for n in nodes if cov.tried(f"{n.path} {n.name}") == least])
    node = cov.node(f"{n.path} {n.name}", n.kind)
    if not t.cfg_saved:                 # restored after every step
        t.sh(f"cfgsave {t.card_path('t_base.cfg')}")
        t.cfg_saved = True
    t.music()
    h = t.health()
    if n.path == "wps":
        t.sh("wps")
        time.sleep(1.5)
    else:
        if not t.home():
            t.fail(f"{n.name}: Home did not reach the main menu first")
            return None
        t.walk_to(n.path, 3000)
    act = t.health().num("act")
    node["visits"] += 1
    if act not in node["acts"]:
        node["acts"].append(act)
    edges = node["edges"]
    k1 = min(CRAWL_KEYS, key=lambda k: (edges.get(k, {}).get("n", 0), t.rng.random()))
    presses = [k1] + ([t.rng.choice(CRAWL_KEYS)] if t.rng.random() < 0.3 else [])
    t.keys(" ".join(f"{k} 1200" for k in presses))
    to = t.health().num("act")
    e = edges.setdefault(k1, {"n": 0, "to": []})
    e["n"] += 1
    if len(presses) == 1 and to not in e["to"]:
        e["to"].append(to)
    cov.save()
    what = f"{n.path} {n.kind} {n.name}: {' '.join(presses)} -> act {to}"
    if not t.home():
        t.fail("Home did not get back")
    t.check_health(h)
    t.sh(f"cfg {t.card_path('t_base.cfg')}")      # undo settings the keys changed
    time.sleep(3)
    return what


@rockbox_scenario("sleep", weight=4, needs={LCD_GATE})
def sleep(t: RockboxTorture) -> str:
    """LCD sleep (the controller's clock gated) and wake by a key, sometimes
    with settings loaded while asleep"""
    t.music()
    h = t.health()
    t.sh(f"cfg {t.card_path('t_sleep.cfg')}")
    time.sleep(9)
    asleep = not t.gate_on()
    again = t.rng.random() < 0.5
    if again:
        t.sh(f"cfg {t.card_path('t_sleep.cfg')}")      # settings applied while asleep
        time.sleep(1)
    t.keys("D 1500")
    awake = t.gate_on()
    t.sh(f"cfg {t.card_path('t_wake.cfg')}")
    if not asleep or not awake:
        t.fail(f"clock gate: asleep {asleep}, awake {awake}")
    t.check_health(h)
    return "sleep/wake" + (", settings loaded asleep" if again else "")


# ---------------------------------------------------------------- recording and radio

@rockbox_scenario("record", weight=8)
def record(t: RockboxTorture) -> str:
    """dev `rec`: a random format, source (FM where the player tunes without
    its screen, FM_TUNE) and channels, sometimes MP3 pre-recording; no
    pcmrec warnings, the time asked recorded, the file readable. A refusal
    for want of heap is a note."""
    fmt = t.rng.choice(["wav", "aiff", "wv", "mp3"])
    src = t.rng.choice(["fm", "fm", "mic"] if FM_TUNE in t.features else ["mic"])
    chans = "mono" if src == "mic" or t.rng.random() < 0.3 else "stereo"
    secs = t.rng.randint(5, 25)
    pre = t.rng.choice([0, 0, 2, 3]) if fmt == "mp3" else 0
    what = f"{secs}s {src} {chans} {fmt} pre {pre}"
    t.home()                            # not from a Recording screen monkey left recording
    t.sh("stop")
    if src == "fm":
        t.fm_tune(t.rng.choice(FM_KHZ))
    h = t.health()
    out = t.sh(f"rec {secs} t_rec {src} {chans} {fmt} {pre}", timeout=secs + pre + 60)
    rec = [int(m[1]) for ln in out if (m := re.search(r"rec: (\d+) ms recorded", ln))]
    want = (secs + pre) * 1000
    if any("not enough memory" in ln for ln in out):
        t.note(f"refused: not enough memory (free {h.num('free')})")
    elif warn := [ln for ln in out if "warnings" in ln]:
        t.fail(warn[0])
    elif not rec or abs(rec[0] - want) > 1500:
        why = [ln for ln in out if ln.startswith("rec")][:3] or out[-3:]
        t.fail(f"recorded {rec[:1]} ms, wanted {want}: {' | '.join(why)}")
    else:
        meta = " ".join(t.sh(f"meta {t.card_path(f't_rec.{fmt}')}", timeout=60))
        m = re.search(r"meta: 1 in \d+ ms: length (\d+) ms", meta)
        if not m or abs(int(m[1]) - rec[0]) > 1500:
            t.fail(f"file: {meta[:120]}")
        what += f", {rec[0]} ms"
    t.sh(f"rm {t.card_path(f't_rec.{fmt}')}")
    if src == "fm":
        t.fm_tune(None)
    t.check_health(h)
    return what


@rockbox_scenario("recui", weight=4)
def recui(t: RockboxTorture) -> str | None:
    """The recording screen through the main menu: start, record, stop,
    leave; a new file in /recordings"""
    before = len(t.sh(f"ls {RECORDINGS}"))
    h = t.health()
    act = t.menu("recording")
    if act != ACT_RECORDING:
        if any("not enough memory" in text for _, text in t.msgs()):
            return f"refused: not enough memory (free {h.num('free')})"
        t.menu_fail(f"main menu -> Recording gave activity {act}")
        return None
    secs = t.rng.randint(3, 15)
    t.keys(f"S {secs * 1000} L+ 1500 L+ 1500")
    time.sleep(2)
    after = len(t.sh(f"ls {RECORDINGS}"))
    if after <= before:
        t.fail(f"no new file ({before} -> {after} lines)")
    if not t.home():                    # left open, it fails every later play
        t.fail("Home did not leave the recording screen")
    # the recording screen drops the health thread's UI ping: no hang
    t.check_health(h, ignore=("ui_late",))
    return f"{secs}s"


@rockbox_scenario("fmui", weight=4)
def fmui(t: RockboxTorture) -> str | None:
    """The FM radio screen through the main menu: decline a first-run
    autoscan, seek, step, leave by Home"""
    t.sh("stop")
    h = t.health()
    act = t.menu("fm")
    if act != ACT_FM:
        t.menu_fail(f"main menu -> FM Radio gave activity {act}")
        t.home()
        return None
    t.keys("L 800 R+ 3000 R 800 L 800 " + "500 " * t.rng.randint(2, 10))
    time.sleep(t.rng.uniform(3, 10))
    if not t.home():
        t.fail("Home did not leave the radio screen")
    t.check_health(h)
    return "radio screen"


# ---------------------------------------------------------------- the card and USB

@rockbox_scenario("fs", weight=4)
def fs(t: RockboxTorture) -> str:
    """Files made and counted after each (`mkcheck`), some deleted, the rest
    counted, made again: a lost or failed create is a filesystem bug"""
    n = t.rng.randint(5, 80)
    d = f"/torture{t.rng.randint(0, 3)}"
    nowrite = " nowrite" if t.rng.random() < 0.3 else ""
    what = f"{d} n={n}{nowrite}"
    h = t.health()
    out = t.sh(f"mkcheck {d} {n}{nowrite}", timeout=900)
    if failed := [ln for ln in out if "failed" in ln or "present after" in ln]:
        t.fail(failed[0])
        return what
    a, b = sorted(t.rng.sample(range(n), 2))
    t.sh(f"delfiles {d} {a} {b}", timeout=600)
    cnt = next((int(ln.split()[-1]) for ln in t.sh(f"count {d}") if ln.startswith("count ")), -1)
    # earlier runs may have left more in D: only a lower bound (with . and ..)
    if cnt < n - (b - a + 1) + 2:
        t.fail(f"deleted {a}..{b}: count {cnt}")
    t.sh(f"mkfiles {d} {n}", timeout=600)
    t.check_health(h)
    return what


@rockbox_scenario("sdodd", weight=4)
def sdodd(t: RockboxTorture) -> str:
    """SD write and read-back through the unaligned (bounce buffer) path"""
    bufkb = t.rng.choice([4, 8, 16, 32])
    kb = bufkb * t.rng.randint(8, 64)
    odd = " odd" if t.rng.random() < 0.7 else ""
    what = f"{kb} KiB {bufkb}{odd}"
    h = t.health()
    out = " ".join(t.sh(f"sdtest {kb} {bufkb}{odd}", timeout=600))
    if "no " in out and "buffer" in out:
        return f"{what}: heap too fragmented for the pieces"
    m = re.search(r"errors (-?\d+)", out)
    if not m or m[1] != "0":
        t.fail(out[:150])
    t.check_health(h)
    return what


@rockbox_scenario("meta", weight=4)
def meta(t: RockboxTorture) -> str | None:
    """Metadata of a random library file (the database scan's parser), in
    under 3 s"""
    if not (track := t.library_track()):
        return "skipped: no music library"
    out = " ".join(t.sh(f"meta {track}", timeout=120))
    m = re.search(r"meta: (\d) in (\d+) ms", out)
    if not m:
        t.fail(out[:120])
    elif int(m[2]) > 3000:
        t.fail(f"slow: {m[2]} ms")
    return track


@rockbox_scenario("missing", weight=2)
def missing(t: RockboxTorture) -> str:
    """A play of a file that is not there, sometimes over music: playback
    stops as at a playlist's end. It sat in "playing" at 0/0 for good, the
    track's metadata never coming (TS17, 2026-11-06)."""
    if t.rng.random() < 0.5:
        t.music()
    path = f"{t.card_dir}/missing-{t.rng.randint(0, 9999)}.mp3"
    h = t.health()
    t.sh(f"play {path}")
    time.sleep(4)
    if (s := t.playing()).audio == AUDIO_PLAYING:
        t.fail(f"still playing ({s.path} at {s.elapsed}/{s.length} ms) after a play "
               "of a missing file")
    t.check_health(h)
    return path


@rockbox_scenario("usb", weight=2)
def usb(t: RockboxTorture) -> str:
    """A simulated unplug and replug during playback"""
    h = t.health()
    t.sh(f"play {CODECTEST}/mp3_320.mp3")
    t.usbcycle()
    t.check_health(h)
    return "usbcycle during playback"


# ---------------------------------------------------------------- Bluetooth

BtCase = Callable[[RockboxTorture], str]


def bt_scenario(case: str, weight: int, bumble: bool = False,
                needs: Iterable[str] = ()) -> Callable[[BtCase], BtCase]:
    """A bt-CASE step (RockboxTorture.bt_case around FN), needing this PC as
    the headset (and NEEDS); BUMBLE: it lends this PC's adapter to a Bumble
    peer"""
    def register(fn: BtCase) -> BtCase:
        def step(t: RockboxTorture) -> str:
            return t.bt_case(fn)
        step.__doc__ = fn.__doc__
        pc = {BT_PC, BT_BUMBLE} if bumble else {BT_PC}
        rockbox_scenario(f"bt-{case}", weight=weight, needs=pc | set(needs))(step)
        return fn
    return register


@bt_scenario("onoff", 3)
def bt_onoff(t: RockboxTorture) -> str:
    """Settings > Bluetooth power with real keys: 2-6 toggles with random
    gaps (some cut a connect short), sometimes with playback stopped; it
    must auto-connect and stream after"""
    stopped = t.rng.random() < 0.3
    if stopped:
        t.sh("stop")                    # the playback buffer is released
    gaps = [t.rng.choice([0, 0.5, 2, 6]) for _ in range(t.rng.choice([2, 2, 4, 6]))]
    what = f"onoff x{len(gaps)} gaps {gaps}" + (" stopped" if stopped else "")
    for i, gap in enumerate(gaps):      # off, on, ...: ends on
        if not t.bt_toggle():
            return what
        off = i % 2 == 0
        s, secs = t.bt_wait(lambda st, off=off: (st.status == BT_OFF) == off, 10)
        if secs is None:
            t.fail(f"{what}: toggle {i + 1}: status {s.status}")
            return what
        if off:
            t.bt_settled(what, settle=False)        # built-in output, setting off
        time.sleep(gap)
    s, secs = t.bt_wait(t.bt_up, BT_CONNECT_S)
    if secs is None:
        t.bt_fail_log(what)
        t.fail(f"{what}: no auto-connect within {BT_CONNECT_S} s: status {s.status}")
        return what
    t.bt_streaming(what)
    return f"{what}, up in {secs:.1f} s"


@bt_scenario("pcdisc", 2)
def bt_pcdisc(t: RockboxTorture) -> str:
    """This PC drops the link mid-stream; the player or the PC reconnects"""
    by_pc = t.rng.random() < 0.5
    what = f"pcdisc, {'the PC' if by_pc else 'the player'} reconnects"
    t.link().disconnect()
    if t.bt_gone(what):
        time.sleep(t.rng.uniform(0, 4))
        secs = t.bt_reconnect(what, by_pc)
        if secs is not None:
            t.bt_streaming(what)
            what += f" in {secs:.1f} s"
    return what


@bt_scenario("pcpower", 0)
def bt_pcpower(t: RockboxTorture) -> str:
    """This PC's adapter off and on mid-stream or mid-connect (off by default:
    the adapter once wedged in PowerState on-disabling until bluetoothd
    restarted; skipped while other devices use the adapter)"""
    pc = t.link()
    if others := pc.others():
        return f"pcpower skipped: {len(others)} other device(s) on the adapter"
    mid = t.rng.random() < 0.3
    off_s = t.rng.choice([0.5, 3, 10, 25])
    what = f"pcpower{' mid-connect' if mid else ''}, off {off_s} s"
    if mid:
        t.sh("bt stack disconnect")
        t.bt_gone(what)
        t.sh(f"bt stack connect {pc.adapter}")
        time.sleep(t.rng.uniform(0.2, 3))
    off, gone = pc.power(False), False
    try:
        gone = off and t.bt_gone(what, BT_GONE_S)
        if gone:
            time.sleep(off_s)
    finally:
        on = pc.power(True)
    if not (off and on):
        return t.pc_stuck(what)
    if not gone:
        return what
    time.sleep(2)
    secs = t.bt_reconnect(what)
    if secs is not None:
        t.bt_streaming(what)
        what += f", back in {secs:.1f} s"
    return what


@bt_scenario("avrcp", 2)
def bt_avrcp(t: RockboxTorture) -> str:
    """15-40 random AVRCP commands from this PC (play, pause, skips, volume,
    stop, seeks), then Play: still linked, streaming"""
    pc = t.link()
    cmds = t.rng.choices(AVRCP, AVRCP_WEIGHTS, k=t.rng.randint(15, 40))
    what = f"avrcp x{len(cmds)}: {' '.join(cmds[:10])} ..."
    for c in cmds:
        pc.avrcp(c)
        time.sleep(t.rng.uniform(0.05, 1.0))
    pc.avrcp("Play")
    time.sleep(2)
    s = t.bt_status()
    if not t.bt_up(s):
        t.fail(f"{what}: link lost: status {s.status}")
    else:
        t.bt_streaming(what)
    return what


@bt_scenario("churn", 2)
def bt_churn(t: RockboxTorture) -> str:
    """A reconnect while tracks, codecs and stops change under it"""
    files = t.codec_files()
    t.sh("bt stack disconnect")
    if not t.bt_gone("churn"):
        return "churn"
    t.sh(f"bt stack connect {t.link().adapter}")
    acts: list[str] = []
    end = time.monotonic() + t.rng.uniform(4, 12)
    while time.monotonic() < end:
        r = t.rng.random()
        if r < 0.5 and files:
            acts.append(t.rng.choice(files))
            t.sh(f"play {CODECTEST}/{acts[-1]}")
        elif r < 0.85:
            acts.append(t.rng.choice(["R", "L", "S", "R+"]))
            t.sh("wps")
            t.keys(acts[-1])
        else:
            acts.append("stop")
            t.sh("stop")
        time.sleep(t.rng.uniform(0.2, 1.5))
    what = "churn: " + " ".join(acts)
    s, secs = t.bt_wait(t.bt_up, BT_CONNECT_S)
    if secs is None:
        t.fail(f"{what}: no connection within {BT_CONNECT_S} s: status {s.status}")
        return what
    if track := t.library_track():        # test files are short
        t.sh(f"play {track}")
        time.sleep(2)
    t.bt_streaming(what)
    return what


@bt_scenario("rate", 2)
def bt_rate(t: RockboxTorture) -> str:
    """The output moves to the headset with tracks at the other rate loaded
    and still buffering: they must play on (a PCM ring sized by the rate was
    remade under them, and every later play sat at 0/0: TS17 torture 12).
    The link drops, a queue at the rate the stream does not use plays on the
    built-in output, the link comes back; it drops again and a file at the
    stream's rate plays."""
    full = " ".join(t.sh("status full"))
    stream_hz = 48000 if (m := re.search(r" freq (\d+)", full)) and m[1] == "48000" else 44100
    other_hz = 44100 if stream_hz == 48000 else 48000
    names = ls_entries(t.sh(f"ls {STRESS}"))
    other = [f"{STRESS}/{e}" for e in names if STRESS_RATES[other_hz].search(e)]
    same = [f"{STRESS}/{e}" for e in names if STRESS_RATES[stream_hz].search(e)]
    if not other or not same:
        t.fail(f"rate: no {other_hz} and {stream_hz} Hz files in {STRESS}")
        return "rate"
    t.rng.shuffle(other)
    queue = other[0]
    for f in other[1:]:                 # as many as the shell's line takes
        if len(f"play {queue};{f}") <= SHELL_LINE:
            queue += f";{f}"
    what = f"rate: {queue.count(';') + 1} at {other_hz} Hz, the stream at {stream_hz}"
    t.sh("bt stack disconnect")
    if not t.bt_gone(what):
        return what
    t.sh(f"play {queue}")
    time.sleep(t.rng.uniform(1, 4))
    if t.bt_reconnect(what) is None:
        return what
    time.sleep(2)
    if (audio := t.health().num("audio")) != AUDIO_PLAYING:
        t.fail(f"{what}: audio {audio} after the output moved to the headset")
        return what
    if not t.bt_streaming(what):
        return what
    t.sh("bt stack disconnect")
    if not t.bt_gone(what):
        return what
    f = t.rng.choice(same)
    t.sh(f"play {f}")
    t.expect_playing(f, f"play {f}")
    if t.bt_reconnect(what) is not None:
        t.bt_streaming(what)
    return f"{what}, then {PurePosixPath(f).name}"


@bt_scenario("disturb", 2)
def bt_disturb(t: RockboxTorture) -> str:
    """A theme switch, a USB cycle, a plugin, browsing, an inquiry or LCD
    sleep while streaming: still linked, streaming"""
    disturbance = t.rng.choice(["theme", "usb", "plugin", "browse", "scan", "sleep"])
    what = f"disturb: {disturbance}"
    if disturbance == "theme":          # stops playback, as upstream
        cfg = t.rng.choice(t.theme_cfgs)
        t.sh(f"cfg {cfg}")
        time.sleep(4)
        what += f" {cfg}"
    elif disturbance == "usb":
        t.usbcycle()
    elif disturbance == "plugin":
        what += f" {plugins(t)}"
    elif disturbance == "browse":
        t.keys("L L L L D S 1000 D S 1000 S 2000 L L L L")
    elif disturbance == "scan":         # an inquiry while streaming
        t.sh("bt stack scan")
        time.sleep(12)
        found = [ln for ln in t.sh("bt stack list") if ln.startswith("bt found")]
        what += f", {len(found)} found"
    else:
        t.sh(f"cfg {t.card_path('t_sleep.cfg')}")
        time.sleep(9)
        t.keys("D 1500")
        t.sh(f"cfg {t.card_path('t_wake.cfg')}")
    s, secs = t.bt_wait(t.bt_up, BT_CONNECT_S)     # the plugin may have needed a boot
    if secs is None:
        t.fail(f"{what}: link lost: status {s.status}")
    else:
        t.bt_streaming(what)
    return what


@bt_scenario("reboot", 1)
def bt_reboot(t: RockboxTorture) -> str:
    """A boot while streaming: it must auto-connect at boot"""
    what = "reboot while streaming"
    t.boot()
    s, secs = t.bt_wait(t.bt_up, BT_CONNECT_S)
    if secs is None:
        t.bt_fail_log(what)
        t.fail(f"{what}: no auto-connect after the boot: status {s.status}")
        return what
    t.bt_streaming(what)
    return what


@bt_scenario("forget", 1)
def bt_forget(t: RockboxTorture) -> str:
    """The player forgets this PC mid-stream, the PC forgets the player, and
    the player pairs with it again (a bluetoothctl agent answers BlueZ's
    prompts): paired, streaming. BlueZ turns down a Just Works pairing from
    a device it still holds a key for (main.conf JustWorksRepairing, default
    never; TS17 torture 8 step 40), so its key goes too, as a user's would."""
    pc = t.link()
    what = "forget the PC, pair again"
    secs = None
    t.sh(f"bt stack forget {pc.adapter}")
    if t.bt_gone(what):
        pc.remove()
        with t.pairing(True):
            secs = t.bt_reconnect(what)
    if not t.bt_paired():
        t.fail(f"{what}: this PC is not among the player's paired devices again")
    if secs is not None:
        t.bt_streaming(what)
        what += f" in {secs:.1f} s"
    return what


@bt_scenario("capture", 2)
def bt_capture(t: RockboxTorture) -> str:
    """The stream as this PC hears it: CAPTURE_S s of the tone's loud part
    recorded from the null sink, both tones, no dropouts"""
    pc = t.link()
    tone = t.tone
    t.sh(f"play {tone}")
    what = f"capture {tone}"
    s, secs = t.bt_wait(lambda st: st.status == BT_STREAMING, 10)
    if secs is None:
        t.fail(f"{what}: not streaming: status {s.status} sink {s.sink}")
        return what
    lo, hi = CAPTURE_FROM_MS
    end = time.monotonic() + 10
    while (((p := t.playing()).path != tone or p.elapsed < lo)
           and time.monotonic() < end):
        time.sleep(0.2)
    if p.path != tone or not lo <= p.elapsed <= hi:
        t.fail(f"{what}: playing {p.path} at {p.elapsed} ms, not the tone at {lo}..{hi} ms")
        return what
    wav = t.step_file("bt-capture", ".wav")
    pcsink.record(wav, pc.sink, t.rng.randint(*CAPTURE_S))
    lines: list[str] = []
    r = analysis.analyse_wav(wav, lines.append)
    t.transcript += ["", f"{wav.name}, from {p.elapsed} ms into the tone:", *lines]
    what += f", {r.seconds:.1f} s from {p.elapsed} ms"
    if bad := capture_problems(r):
        t.fail(f"{what}: " + "; ".join(bad))
    return what


def capture_problems(r: analysis.WavAnalysis) -> list[str]:
    """What is wrong with a capture of the tone's loud part"""
    bad = [f"channel {ch}: tone {hz and round(hz)} Hz, want {want:g}"
           for ch, (hz, want) in enumerate(zip(r.medians, TONE_HZ, strict=False))
           if hz is None or abs(hz - want) > want * TONE_HZ_TOL]
    if r.seconds < min(CAPTURE_S) - 1:
        bad.append(f"only {r.seconds:.1f} s recorded")
    gaps = [(round(s, 2), round(ms)) for s, ms in r.gaps if ms >= CAPTURE_GAP_MS]
    if gaps:
        bad.append(f"{len(gaps)} dropouts (s, ms): {gaps[:5]}")
    return bad


def peer_failure(log: Path) -> str:
    """A failed Bumble peer's last words"""
    try:
        lines = [ln for ln in log.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return "no peer log"
    return " | ".join(lines[-4:])[:300]


@bt_scenario("headset", 2, bumble=True)
def bt_headset(t: RockboxTorture) -> str:
    """This PC's adapter as a headset (`arb bt-peer pcheadset`) that pages
    the player 3-8 times: role switches, links that vanish once the music
    plays, re-pages that vanish again. Every cycle must reach the music;
    then the player pairs with this PC again and streams."""
    pc = t.link()
    if others := pc.others():
        return f"headset skipped: {len(others)} other device(s) on the adapter"
    cycles, seed = t.rng.randint(3, 8), t.rng.randrange(1 << 30)
    out = t.step_file("headset", "")
    what = f"headset x{cycles} seed {seed} ({out.name})"
    if not t.lend_adapter(what):
        return what
    from ..btpeer import pcheadset  # noqa: PLC0415 (Bumble)
    opts = HeadsetOptions(out=out, adapter=pc.transport, player=pc.player, cycles=cycles,
                          switch_after=(0.0, 6.0), switch_share=0.8, lose_share=0.4,
                          relose_share=0.4, repage_wait=40.0, seed=seed)
    limit = cycles * HEADSET_CYCLE_S + HEADSET_SPARE_S
    t.sh("bt stack sink bt")
    if track := t.library_track():
        t.sh(f"play {track}")
    try:
        with t.mirror():
            pcheadset.main(opts, limit=limit, echo=False)
    except TimeoutError:
        t.fail(f"{what}: the cycles still ran after {limit} s")
    finally:
        t.take_back_adapter(what)
    try:
        lines = (out / "cycles.txt").read_text().splitlines()
    except OSError:
        lines = []
    bad = [ln.split(" ", 2)[2] for ln in lines if not re.search(r"cycle \d+: ok;", ln)]
    lost = t.bt_lost = sum("vanished" in ln for ln in lines)
    what += f": {len(lines) - len(bad)} of {cycles} ok, {lost} lost"
    if bad or len(lines) < cycles:
        t.fail(f"{what}: " + ("; ".join(bad[:3]) or f"{len(lines)} cycles ran"))
    return what


@bt_scenario("hid", 2, bumble=True, needs={BT_HID})
def bt_hid(t: RockboxTorture) -> str:
    """This PC's adapter as a DualShock 4 or keyboard (`arb yp3 bt-peer
    hid`) that the player scans for and pairs with, then 4-10 random keys
    (never A), sometimes with music and 30 s more connected: the player
    must see each key. Then it pairs with this PC again and streams."""
    pc = t.link()
    if others := pc.others():
        return f"hid skipped: {len(others)} other device(s) on the adapter"
    kind = t.rng.choice(["ds4", "keyboard"])
    press = t.rng.choices(HID_KEYS + (HID_DS4_KEYS if kind == "ds4" else []),
                          k=t.rng.randint(4, 10))
    play = t.library_track() if t.rng.random() < 0.4 else None
    out = t.step_file("hid", "")
    what = f"hid {kind} {' '.join(press)!r}{' playing' if play else ''} ({out.name})"
    if not t.lend_adapter(what):
        return what
    from ..btpeer import hid  # noqa: PLC0415 (Bumble)
    opts = HidOptions(out=out, kind=kind, press=press, just_works=kind == "keyboard",
                      forget=True, play=play, stay=30.0 if play else 0.0)
    try:
        if hid.main_air(opts, t.shell, pc.transport, pc.player, limit=HID_LIMIT_S, echo=False):
            t.fail(f"{what}: {peer_failure(out / 'peer.log')}")
    except TimeoutError:
        t.fail(f"{what}: still running after {HID_LIMIT_S} s")
    finally:
        t.keys("UL 500")                # Home: the keys may have left a menu open
        t.take_back_adapter(what)
    return what


# ---------------------------------------------------------------- commands

@command("crawl-coverage", help="the torture crawl's graph of the UI: coverage, or Graphviz dot",
         needs={"shell"},
         args=[arg("--graph", action="store_true", help="the graph as Graphviz dot")])
def crawl_coverage(ctx: Context, args: Namespace) -> int:
    """From [targets.NAME] menu_coverage (default <paths.build>/menu-coverage-NAME.json)"""
    cov = Coverage(coverage_file(ctx.t))
    print("\n".join(cov.dot() if args.graph else cov.summary()))
    return 0
