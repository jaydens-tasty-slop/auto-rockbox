"""The YP3's torture run: its dev shell's commands and replies (apps/yp3_dev.c),
the health line's YP3 fields, what every step is checked for, and the
scenarios (auto_rockbox.suites.torture runs them).

    arb yp3 torture build/torture-elf/rockbox.elf build/torture/RUN --hours 6

The card is set up as for the dev loop (the port's docs/testing.md, Card
setup): /codectest/ test files (`arb media`), /stress/ and
"/Playlists/Stress test.m3u8" (`arb media-stress`), /.rockbox/rocks/ plugins
and the SD codecs built with the core, a music library (artist/album
folders), a built database, the cabbiev2 theme, /video/ videos,
/yp3dev/tone.mp3 (1 kHz left, 1.5 kHz right, 12 dB down after 10 s). Each
boot pushes the settings files of CFGS to /yp3dev/ and sets Charge Limit 80 %.

After every step: the messages it put on the screen (`msgs`: splashes, yes/no
prompts, plugin errors) fail it when they read as errors (ERROR_MSG, less the
scenario's EXPECTED_MSG and the plugins known to fail without their data);
a track played from its start must decode to the same hash as earlier plays
of that file over as many frames; a Video Player play must not underrun,
drop over VIDEO_DROP_MAX of its pictures or lose its second core; the
charger must be off above the charge limit; a database build must end
before the next step.

The bt-* scenarios make this PC the player's headset, through BlueZ, with
the player's stream on a null sink for the whole run (never the speakers).
They are in a run only with the player's address configured, and need it
paired with this PC (`arb bt-peer pcsink`) and a Bluetooth build:

    [bluetooth]
    player = "AA:BB:CC:DD:EE:FF"    # the player's BD_ADDR
    sink = "arb_null"               # the null sink its stream goes to (made if missing)

`bt-pcpower` (this PC's adapter off and on: it once wedged until bluetoothd
restarted) is off unless --weights gives it a weight. The cases that need a
peer of their own (the headset, HID device, stream capture and pairing
agent for forget-and-pair of `arb bt-peer`, auto_rockbox.btpeer) are not
ported yet.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from argparse import Namespace
from collections import Counter
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar

from ... import fb, linein
from ...bt import bluez
from ...elf import Elf
from ...errors import ArbError
from ...registry import Context, arg, command
from ...suites.torture import (
    Health,
    Lost,
    Records,
    TortureError,
    TortureSuite,
    player_suite,
    scenario,
)
from ...target import RunRequest, Target
from . import SHELL_VID, Yp3, Yp3Run
from .retained import decode_fault, decode_log, decode_threads
from .rom import wait_rom
from .shot import screenshot

YP3 = {"yp3"}
BT_PC = "bt-pc"                 # suite feature: this PC can be the player's headset

# The card
CFG_DIR = "/yp3dev"             # settings files pushed at each boot; recordings
CFGS = {
    "t_sleep.cfg": "backlight timeout: 3\nbacklight timeout plugged: 3\n"
                   "lcd sleep after backlight off: 2\n",
    "t_wake.cfg": "backlight timeout: 15\nbacklight timeout plugged: 15\n"
                  "lcd sleep after backlight off: 5\n",
    "t_limit.cfg": "charge limit: 80\n",       # CHARGE_LIMIT
    "t_default.cfg": "wps: -\nsbs: -\nfms: -\niconset: -\nviewers iconset: -\n"
                     "backdrop: -\nforeground color: FFFFFF\nbackground color: 000000\n"
                     "selector type: bar (gradient)\nstatusbar: top\nui viewport: -\n",
}
THEME_CFGS = ["/.rockbox/themes/cabbiev2.cfg", "/.rockbox/themes/rockbox_failsafe.cfg",
              f"{CFG_DIR}/t_default.cfg"]
CODECTEST = "/codectest"
CODEC_EXT = re.compile(r"\.(mp3|flac|m4a|tta|aiff|au|wav|wv|ac3|spx|ape|ogg|opus)$")
# known too slow or over a documented limit (docs/codecs.md: Vorbis pages over
# ~10 KB, Opus pages over 30 KB)
SLOW_CODECS = ("wv_24", "vorbis_q", "vorbis_long", "vq3_1s", "opus_510")
PROBE = f"{CODECTEST}/flac_16_44.flac"          # an SD codec: are the modules this build's?
UNSUPPORTED = f"{CODECTEST}/wma_320.wma"        # WMA: no codec on master
STRESS = "/stress"
STRESS_PLAYLIST = "/Playlists/Stress test.m3u8"
ROCKS = "/.rockbox/rocks"
PLUGIN_DIRS = ("games", "apps", "demos")
PLUGIN_SKIP = ("battery_bench.rock", "lastfm_scrobbler.rock")
VIDEO_DIR = "/video"
VIDEO_PLAYER = f"{ROCKS}/viewers/videoplayer.rock"
VIDEO_EXT = (".avi", ".mkv", ".mp4", ".mov")
RECORDINGS = "/recordings"
NOT_LIBRARY = ("codectest", "yp3dev")           # and torture*: the fs scenario's
TONE = "/yp3dev/tone.mp3"       # -15 dBFS for 10 s, then -27 dBFS

# The player
MAIN_MENU = ["files", "database", "resume", "settings", "recording", "fm",
             "playlists", "plugins", "system", "shortcuts"]    # root_menu.c's order
# enum current_activity (misc.h), `health` act
ACT_MAINMENU, ACT_RECORDING, ACT_FM, ACT_DATABASE, ACT_PLUGIN = 1, 3, 4, 8, 14
ACT_NAMES = ["?", "main menu", "WPS", "recording", "FM", "playlist viewer",
             "settings", "files", "database", "plugins", "quickscreen", "pitch",
             "option select", "playlists", "plugin", "context menu", "system",
             "time/date", "bookmarks", "shortcuts", "id3 info", "usb"]
AUDIO_PLAYING, AUDIO_PAUSED = 1, 3          # audio_status()
LCD_GATE_ACK = 0x40080068       # clock gate 0x5c (LCD controller): bit 28
FM_KHZ = (98100, 101500, 104300, 92500, 88700)
VOLUME_DB = -20
CHARGE_LIMIT = 80               # % (t_limit.cfg)
SHELL_LINE = 127                # apps/yp3_dev.c sh_rx[128]: longer lines are cut

# A message (splash, prompt) that reports a failure; and those a scenario
# provokes on purpose
ERROR_MSG = re.compile(r"(?i)error|fail|not enough|memory|cannot|can't|couldn't|unable|"
                       r"invalid|corrupt|incompatible|wrong|not found|unsupported|leak|"
                       r"denied|too (big|large|long)|no file|panic|bad ")
EXPECTED_MSG = {"record": re.compile(r"(?i)not enough memory"),
                "recui": re.compile(r"(?i)not enough memory"),
                # music holds the heap the database menus need: the prompt
                "db": re.compile(r"(?i)not enough memory for / the database"),
                "dbdeep": re.compile(r"(?i)not enough memory for / the database|"
                                     r"selection too big"),
                # cabbiev2 leaves too little for Vorbis or Opus
                "theme": re.compile(r"(?i)not enough memory to play|leaves little memory")}
# Plugins that end in "Plugin returned error" when started without their data
# or a file (upstream behaviour): the splash that explains it, None if none
KNOWN_PLUGIN_ERRORS: dict[str, str | None] = {
    "dict.rock": r"Failed to open index",                      # no dictionary
    "xworld.rock": r"data files missing|res_allocMemBlock",     # no game data
    "mp3_encoder.rock": None,                                   # a .wav viewer
    "imageviewer.rock": r"No supported files|No file|Unsupported file",
    "random_folder_advance_config.rock": r"folder_advance_list",
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
# a keyboard (long Left) and Text Editor's unsaved-changes menu
PLUGIN_EXITS = ["SU* 800 U S", "U S", "4000 SU* 800 U S", "U S", "U S L S+ U S L L",
                "L+ S+", "U+ S", "D S U S", "L+ 300 L 300 U S"]
# Before Home: Back presses, then plugin exits, for screens that ignore Home
HOME_FIRST = ["", "L 300 L 300 L 300 ", "SU* 800 U S ", "U S L S+ U S L L ", "L+ S+ ", "U+ S "]
# The crawl: menu items it never runs (they lose data, power off or need hours)
CRAWL_SKIP = re.compile(r"(?i)shut ?down|power off|reboot|reset|format|delete|erase|"
                        r"clear|initiali[sz]e|rebuild|update now|export|import|"
                        r"usb|write .*cfg|save .*(theme|settings)")
CRAWL_KEYS = ["U", "D", "L", "R", "S", "U+", "D+", "L+", "R+", "S+", "UL"]

# Bluetooth
BT_CONNECT_S = 25               # connect and reconnect deadline
BT_GONE_S = 30                  # link loss seen (supervision timeout 20 s)
BT_SETTLE_S = 4                 # audio routing follows a status change
BT_OFF, BT_ON, BT_CONNECTED, BT_STREAMING = range(4)    # enum bt_status
SINK_BUILTIN, SINK_BT = 0, 1    # enum pcm_sink_ids
BT_BITPOOL_OK = 47              # the stream to this PC climbs back to this...
BT_BITPOOL_S = 45               # ...within this (at the desk: 35 -> 53 in ~25 s)
BT_LINK_TBL = 0x804128          # ROM link address table: 7 x {u8 in use, BD_ADDR}
AVRCP = ["Play", "Pause", "Next", "Previous", "VolumeUp", "VolumeDown", "Stop",
         "FastForward", "Rewind"]
AVRCP_WEIGHTS = [4, 4, 3, 3, 2, 2, 1, 1, 1]


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


def message_problems(scenario: str, msgs: list[tuple[str, str]]) -> list[str]:
    """The messages that read as errors, but those SCENARIO provokes and the
    known plugin errors (a plugin's "returned error" and the splash before it)"""
    known = [why for kind, text in msgs if kind == "plugin" and "returned error" in text
             for rock, why in KNOWN_PLUGIN_ERRORS.items() if f"/{rock} " in text]
    expected = EXPECTED_MSG.get(scenario)
    bad = []
    for kind, text in msgs:
        if kind in ("decode", "video", "lost") or not (kind == "plugin" or ERROR_MSG.search(text)):
            continue
        if expected and expected.search(text):
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


def fm_periods(text: str) -> tuple[int, int] | None:
    """`fmmon`: (capture periods, overruns)"""
    m = re.search(r"periods=(\d+) consumed=\d+ over=(\d+)", text)
    return (int(m[1]), int(m[2])) if m else None


def fm_consumer(m0: str, m1: str) -> str:
    """What drained the capture between two `fmmon` replies: the mixer
    channel, the DAC, the mixer's longest gap"""
    d0, d1 = (re.search(r"mixer chan=(\d+) pcm playing=(\d+) dac periods=(\d+)", m)
              for m in (m0, m1))
    gaps = re.search(r"mixer gaps: longest (\d+) ms, (\d+) over two periods, "
                     r"last call (\d+) ms ago", m1)
    why = ""
    if d0 and d1:
        chan = ("stopped", "playing", "paused")[int(d1[1])] if int(d1[1]) < 3 else d1[1]
        why += (f"; mixer channel {chan}, pcm playing {d1[2]}, "
                f"DAC periods +{int(d1[3]) - int(d0[3])}")
    if gaps:
        why += (f"; longest mixer gap {gaps[1]} ms, {gaps[2]} over two periods, "
                f"last call {gaps[3]} ms ago")
    return why


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


def link_table(words: list[int]) -> list[str]:
    """The BD_ADDRs in use in the controller's link address table (BT_LINK_TBL)"""
    raw = b"".join(w.to_bytes(4, "little") for w in words)
    return [":".join(f"{b:02X}" for b in reversed(raw[i + 1:i + 7]))
            for i in range(0, min(len(raw), 49) - 6, 7) if raw[i] == 1]


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
        lines = ["digraph yp3 {", "  rankdir=LR; node [shape=box, fontsize=9];"]
        for k, c in self._ordered():
            lines.append(f'  "{k}";')
            lines += [f'  "{k}" -> "{act(to)}" [label="{key}", fontsize=8];'
                      for key, e in c.get("edges", {}).items() for to in e["to"]]
        return [*lines, "}"]


def coverage_file(t: Target) -> Path:
    return t.conf.path("menu_coverage", str(t.ws.build_dir / "menu-coverage.json"))


# ---------------------------------------------------------------- this PC as the headset

class PcLink:
    """This PC's Bluetooth adapter as the player's headset, through BlueZ
    (bluetoothctl, busctl). While routing, the player's stream is kept on a
    null sink (made if missing, removed again)."""

    def __init__(self, player: str, sink: str) -> None:
        self.player, self.sink = player.upper(), sink
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
        """The adapter, the player among its paired devices, its D-Bus path"""
        try:
            tree = subprocess.run(["busctl", "--list", "tree", "org.bluez"], capture_output=True,
                                  text=True, timeout=20, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return False
        m = re.search(r"Controller ([0-9A-F:]{17})", self.ctl("show"))
        if not m or self.player not in self.ctl("devices", "Paired"):
            return False
        self.adapter = m[1]
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

@player_suite("yp3")
class Yp3Torture(TortureSuite):
    """The torture run on the YP3. Config ([targets.yp3]): menu_coverage, the
    crawl's graph (default <paths.build>/menu-coverage.json); [bluetooth]
    player and sink for the bt-* scenarios."""

    health_words: ClassVar[tuple[str, ...]] = ("plugin",)
    crash_lines: ClassVar[str] = r"\bHANG:"      # the heartbeat watchdog's last word
    line_max: ClassVar[int] = SHELL_LINE

    def __init__(self, target: Target, image: Path, **kw: Any) -> None:
        super().__init__(target, image, **kw)
        if not isinstance(target, Yp3):
            raise TypeError(f"{target.name}: not a YP3")
        self.yp3 = target
        self.shell = target.shell(image)        # names the addresses in `ticks:` by IMAGE
        self.msg_next = 1               # the next `msgs` number (from 1 after a boot)
        self.pending: list[tuple[str, str]] = []    # messages a scenario read early
        self.messages: Counter[tuple[str, str]] = Counter()
        self.msgs_lost = 0
        self.decodes = DecodeHashes()
        self.videos = VideoStats()
        self.linein_db: list[float] = []
        self.battery: list[tuple[float, int, int, bool]] = []
        self.tree: list[MenuNode] | None = None
        self.cfg_saved = False
        self.coverage = Coverage(coverage_file(target))
        bt = target.ws.section("bluetooth")
        player = bt.get("player")
        self.pc = PcLink(player, bt.string("sink", "arb_null")) if isinstance(player, str) else None
        if self.pc:
            self.features = frozenset({BT_PC})
        self.bt_known = False
        self.bt_off = ""                # why the bt-* scenarios do nothing this run
        self.bt_recovering = False

    # ---- the suite's hooks

    def start(self, out: Path) -> None:
        if self.pc:         # the player streams to this PC from its boot on
            self.pc.start_routing()
        super().start(out)

    def release(self) -> None:
        if self.pc:
            self.pc.power(True)         # a bt-pcpower step may have died
            self.pc.stop_routing()

    def boot_request(self) -> RunRequest:
        pushes = []
        for name, text in CFGS.items():
            (self.out / name).write_text(text)
            pushes.append((self.out / name, f"{CFG_DIR}/{name}"))
        return Yp3Run(self.image, 0, usb=True, settings={"speaker": "0"}, pushes=pushes)

    def prepare(self) -> None:
        self.msg_next, self.tree = 1, None
        self.sh("repeat 0")
        self.sh(f"vol {VOLUME_DB}")
        self.sh("dryoff")               # a menu power-off returns to ROM USB
        self.sh(f"cfg {CFG_DIR}/t_limit.cfg")
        super().prepare()
        # the boot-time database update stops playback and its commit takes
        # all free RAM: steps start after it
        for _ in range(300):
            if not self.health().num("db"):
                break
            time.sleep(2)

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
        return self.take_messages() + self.charge_check()

    def busy(self, h: Health) -> str | None:
        return "a database build" if h.num("db") else None

    def settled(self, h: Health) -> bool:
        return (h.num("audio") == 0 and h.text("plugin") == "-" and h.num("db") == 0
                and h.num("act") == ACT_MAINMENU)

    def wait_host(self, timeout: float) -> bool:
        return wait_rom(time.monotonic() + timeout) is not None

    def records(self) -> Records:
        raw = self.yp3.read_retained()
        names = self.yp3.function_names(self.image)
        log = decode_log(raw, names)
        return [("fault", decode_fault(raw, self.yp3.symbolizer(self.image).names)),
                ("threads", decode_threads(raw, Elf(self.image), names)),
                ("dev log (tail)", "\n".join(log.splitlines()[-120:]))]

    def expected_return(self, records: Records) -> str | None:
        if "power_off() (dry run" in dict(records).get("dev log (tail)", ""):
            return "SHUTDOWN (menu power-off, expected)"
        return None

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
        self.sh(f"cfg {CFG_DIR}/t_default.cfg")     # the default look and timeouts
        self.sh(f"cfg {CFG_DIR}/t_wake.cfg")

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
        bad += message_problems(self.step.name, msgs)
        return [f"messages: {'; '.join(bad)[:300]}"] if bad else []

    def charge_check(self) -> list[str]:
        """The charger stays off above the charge limit; a reading every ~10 min"""
        b = parse_battery(self.sh("bat"))
        if b is None:
            return []
        mv, level, charging = b
        if not self.battery or time.time() - self.battery[-1][0] > 600:
            self.battery.append((time.time(), mv, level, charging))
        if charging and level > CHARGE_LIMIT:
            return [f"charging at {level}% ({mv} mV), limit {CHARGE_LIMIT}%"]
        return []

    # ---- playback

    def playing(self) -> Status:
        return parse_status(self.sh("status"))

    def expect_playing(self, path: str | None = None, marker: str | None = None) -> bool:
        """Fails the step unless playback progresses (playing, elapsed up by
        1.5 s over 3 s), on PATH if given (not skipped), with no codec error
        logged after the line holding MARKER"""
        s1 = self.playing()
        time.sleep(3)
        s2 = self.playing()
        bad = []
        if s2.audio != AUDIO_PLAYING or s2.elapsed - s1.elapsed < 1500:
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
                if CODEC_EXT.search(f) and not f.startswith(SLOW_CODECS)]

    def pick_codec_file(self) -> str | None:
        files = self.codec_files()
        if not files:
            self.fail(f"no test files in {CODECTEST}")
            return None
        return self.rng.choice(files)

    def wait_within(self, wait: float) -> float:
        """WAIT, or less so a playing check (3 s) ends before the track does
        (some test files are 12 s)"""
        length = self.playing().length / 1000
        return max(1, min(wait, length - 5)) if length else wait

    def library_track(self, album: bool = False) -> str | None:
        """A random track of the music library (artist/album folders); ALBUM:
        never one of the songs at the card's root"""
        path = ""
        for _ in range(3):
            entries = ls_entries(self.sh(f"ls {path or '/'}"))
            songs = [e for e in entries if re.search(r"\.(mp3|flac)$", e, re.I)]
            if songs and (path or not album):
                return f"{path}/{self.rng.choice(songs)}"
            dirs = [e.rstrip("/") for e in entries if e.endswith("/") and not e.startswith(".")
                    and e.rstrip("/") not in NOT_LIBRARY and not e.startswith("torture")]
            if not dirs:
                return None
            path = f"{path}/{self.rng.choice(dirs)}"
        return None

    def music(self) -> None:
        """A library track playing under a UI scenario, so health catches the
        hitches the UI causes"""
        if self.health().num("audio") != AUDIO_PLAYING and (track := self.library_track()):
            self.sh(f"play {track}")
            time.sleep(2)

    # ---- screens and keys

    def in_plugin(self) -> bool:
        """A plugin runs (one `run` started, or one opened from the menus):
        it holds the module arena, so codec loads fail meanwhile"""
        h = self.health()
        return h.text("plugin") != "-" or h.num("act") == ACT_PLUGIN

    def close_plugin(self) -> bool:
        """Out of any running plugin; False if no exit worked"""
        for script in PLUGIN_EXITS:
            if not self.in_plugin():
                return True
            self.keys(script)
        return not self.in_plugin()

    def no_plugin(self) -> None:
        """Before a playback check: a plugin left open would fail it for the
        wrong reason; boot again if it will not close"""
        if not self.close_plugin():
            self.boot()

    def ui_idle(self, limit: float = 90) -> None:
        """Waits while the UI thread misses its 10 s ping (e.g. a free-space
        rescan, ~35 s on a 32 GB card): keys meanwhile are lost"""
        end = time.monotonic() + limit
        while time.monotonic() < end and self.health().num("ui_age") > 10:
            time.sleep(1)

    def home(self) -> bool:
        """Home (Up+Left) twice: the main menu with its first item selected
        (dev builds: HAVE_HOME_SELECTS_FIRST_ITEM). Screens that ignore Home
        get Back presses and plugin exits first; if nothing works, boot again
        and return False."""
        for first in HOME_FIRST:
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
        self.keys("D 150 " * MAIN_MENU.index(item) + "S 2500")
        act = self.health().num("act")
        if item == "database" and act == ACT_MAINMENU:
            self.keys("S 2500")         # out of heap while playing: "Stop playback?" yes
            act = self.health().num("act")
        return act

    def walk_to(self, path: str, last_wait: int) -> None:
        """Down and Select down a `menutree` PATH from the main menu"""
        steps = path.split(".")
        for i, k in enumerate(steps):
            self.keys("D 150 " * int(k) + f"S {last_wait if i == len(steps) - 1 else 1500}")

    def menu_fail(self, problem: str) -> None:
        """A main menu entry that did not open: its screen kept beside the step"""
        png = self.step_file("menu", ".png")
        try:
            im, _ = screenshot(lambda line: self.reply(line, 60), self.target.lcd)
            fb.scaled(im).save(png)
            self.fail(f"{problem} ({png.name})")
        except ArbError as e:
            self.fail(f"{problem} (no screenshot: {e})")

    def menu_tree(self) -> list[MenuNode]:
        """The dev `menutree` dump, once per boot"""
        if self.tree is None:
            self.tree = parse_menutree(self.sh("menutree", timeout=60))
        return self.tree

    def gate_on(self) -> bool:
        """The LCD controller's clock runs (off while the panel sleeps)"""
        words = r32_words(self.sh(f"r32 {LCD_GATE_ACK:#x}"))
        return bool(words and words[0] >> 28 & 1)

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
                   + (" (Rockbox is in USB mass storage mode)" if mass_storage_up() else ""))

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
            raise TortureError("no [bluetooth] player configured")
        return self.pc

    def bt_ready(self) -> bool:
        """This PC paired in BlueZ, the player's radio on and connected to it"""
        pc = self.link()
        if self.bt_off:
            return False
        if not self.bt_known:
            if not pc.find():
                self.bt_off = (f"BlueZ has no adapter, or no paired {pc.player} "
                               "([bluetooth] player)")
                return False
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
        for by_pc in (False, True):
            # the player's link or page to another device (auto-connect takes
            # the most recent one), or its own page of the PC, would keep the
            # next try from getting through
            if self.bt_status().addr and (by_pc or s.addr != pc.adapter):
                self.sh("bt stack disconnect")
                self.bt_wait(lambda st: st.addr is None, 8)     # page timeout 5.12 s
            if self.bt_reconnect("setup", by_pc, quiet=True) is not None:
                if by_pc:
                    self.note(f"the player's connect failed (linked to {s.addr or 'nothing'}); "
                              "the PC's worked")
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
        paired = any(ln.startswith("bt paired") and pc.adapter in ln
                     for ln in self.sh("bt stack list"))
        self.fail(f"bt setup: no connection from either side within {BT_CONNECT_S} s: status "
                  f"{s.status}, player linked to {s.addr or 'nothing'}, paired {paired}, PC sees "
                  f"{'connected' if pc.connected() else 'no link'}")
        if not paired:
            self.bt_off = "the player no longer knows this PC: pair them again (arb bt-peer pcsink)"
        return False

    def bt_toggle(self) -> bool:
        """Settings > Bluetooth > "Bluetooth: On/Off" with real keys"""
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

    def bt_link_table(self) -> list[str]:
        return link_table(r32_words(self.sh(f"r32 {BT_LINK_TBL:#x} 13")))

    def bt_fail_log(self, what: str) -> None:
        """A failed connect: the device log (with the player's connect traces),
        the LMP trace and what bluetoothctl said, beside the step"""
        out = [f"{time.strftime('%F %T')} {what}", "", "--- bluetoothctl connect",
               self.link().last_connect or "(none)"]
        for cmd in ("log", "bt ctrl lmp"):      # the LMP ring outlasts the 4 KB log
            out.append(f"--- sh {cmd}")
            try:
                out += self.sh(cmd)
            except Lost as e:
                out.append(f"(no reply: {e})")
        try:
            out.append(f"--- link table in use: {' '.join(self.bt_link_table()) or 'none'}")
        except Lost as e:
            out.append(f"--- link table: no reply: {e}")
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
        """Play again (the policy pauses when a headset goes away)"""
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

    def pc_stuck(self, what: str) -> str:
        """This PC's adapter did not reach the power state asked for: not the
        player's fault, and nothing more to test with it"""
        self.fail(f"{what}: this PC's adapter is stuck in PowerState "
                  f"{self.link().power_state} (needs a bluetoothd restart): bt off for this run")
        self.bt_off = "this PC's adapter is stuck"
        return what

    def bt_case(self, case: Callable[[Yp3Torture], str]) -> str:
        """A bt-* step: ready, music, the case; then the output settled, the
        bitpool back, health"""
        if not self.bt_ready():
            return f"skipped: {self.bt_off}" if self.bt_off else "setup failed"
        self.music()
        h = self.health()
        what = case(self)
        self.sh(f"vol {VOLUME_DB}")            # after AVRCP volume keys
        if self.bt_off:
            return what
        self.bt_settled(what)
        if self.bt_status().status == BT_STREAMING:
            self.bt_bitpool(what)
        self.check_health(h)
        return what


def mass_storage_up() -> bool:
    """Whether the dev build's USB is a mass storage device (class 08)"""
    for intf in Path("/sys/bus/usb/devices").glob("*:1.0"):
        with suppress(OSError):
            if ((intf.resolve().parent / "idVendor").read_text().strip() == SHELL_VID
                    and (intf / "bInterfaceClass").read_text().strip() == "08"):
                return True
    return False


# ---------------------------------------------------------------- playback scenarios

@scenario("codecs", weight=12, target=YP3)
def codecs(t: Yp3Torture) -> str | None:
    """A /codectest file, mostly on Now Playing (the worst case): it plays on"""
    if not (f := t.pick_codec_file()):
        return None
    t.no_plugin()
    h = t.health()
    t.sh(f"play {CODECTEST}/{f}")
    if t.rng.random() < 0.7:
        t.sh("wps")
    time.sleep(t.wait_within(t.rng.uniform(8, 18)))
    t.check_health(h)
    t.expect_playing(f"{CODECTEST}/{f}", f"play {CODECTEST}/{f}")
    return f


@scenario("decode", weight=10, target=YP3)
def decode(t: Yp3Torture) -> str | None:
    """A /codectest file from its start while the UI, the SD card or nothing
    else works; after_step compares its decode hash with earlier plays"""
    if not (f := t.pick_codec_file()):
        return None
    t.no_plugin()
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


@scenario("linein", weight=4, target=YP3)
def linein_tone(t: Yp3Torture) -> str | None:
    """The wired output on this PC's Line In (`arb linein serve`): the tone
    file's two tones whole, at a steady level, that of earlier steps"""
    directory = linein.directory(t.target.ws)
    latest = directory / "latest.json"
    if not latest.exists() or time.time() - latest.stat().st_mtime > 5:
        return "skipped: no Line In capture running (arb linein serve)"
    t.no_plugin()
    t.sh("stop")                        # a sink switch under a playing track underruns
    bt = parse_bt_status(t.sh("bt stack status"))
    to_bt = bt is not None and bt.sink == SINK_BT
    if to_bt:
        t.sh("bt stack sink builtin")
    t.sh(f"vol {VOLUME_DB}")            # skips, monkey and AVRCP move it
    h = t.health()
    since = datetime.now(UTC)
    t.sh(f"play {TONE}")
    time.sleep(1.5)
    s0, t1 = t.playing(), time.monotonic()
    if s0.path != TONE:
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


@scenario("video", weight=4, target=YP3)
def video(t: Yp3Torture) -> str | None:
    """Video Player on a random /video file, random keys, then out (its
    stats are judged with the messages)"""
    vids = [v for v in ls_entries(t.sh(f"ls {VIDEO_DIR}")) if v.lower().endswith(VIDEO_EXT)]
    if not vids:
        return f"skipped: no videos in {VIDEO_DIR}"
    v = t.rng.choice(vids)
    t.no_plugin()
    h = t.health()
    t.sh(f"run {VIDEO_PLAYER} {VIDEO_DIR}/{v}")
    time.sleep(t.rng.uniform(4, 20))
    t.keys(" ".join(t.rng.choices(["U", "D", "L", "R", "S", "200", "1000"], k=10)))
    time.sleep(2)
    # Quit is the menu's last item (Up wraps to it), but a landscape video
    # turns the keys with the picture: Up is Right then
    for up in ("U", "R"):
        if t.in_plugin():
            t.keys(f"S+ 300 {up} 300 S")
            time.sleep(1)
    if not t.close_plugin():
        t.fail("did not exit")
        t.boot()
        return v
    t.check_health(h)
    return v


@scenario("codecmix", weight=8, target=YP3)
def codecmix(t: Yp3Torture) -> str | None:
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
    t.no_plugin()
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


@scenario("shuffle", weight=8, target=YP3)
def shuffle(t: Yp3Torture) -> str:
    """The codec stress playlist shuffled, with skips and seeks across
    codecs: the last track must play"""
    t.no_plugin()
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


@scenario("skips", weight=6, target=YP3)
def skips(t: Yp3Torture) -> str | None:
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


@scenario("folders", weight=4, target=YP3)
def folders(t: Yp3Torture) -> str | None:
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


@scenario("longplay", weight=4, target=YP3)
def longplay(t: Yp3Torture) -> str | None:
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


@scenario("theme", weight=4, target=YP3)
def theme(t: Yp3Torture) -> str:
    """A theme switch (stops playback, as upstream), then play on Now Playing"""
    cfg = t.rng.choice(THEME_CFGS)
    t.sh(f"cfg {cfg}")
    time.sleep(4)
    if track := t.library_track():
        h = t.health()
        t.sh(f"play {track}")
        t.sh("wps")
        time.sleep(t.rng.uniform(8, 15))
        t.keys("UL 500")
        t.check_health(h)
    t.sh(f"cfg {CFG_DIR}/t_default.cfg")   # left on, cabbiev2 refuses Vorbis and Opus later
    return cfg


# ---------------------------------------------------------------- UI scenarios

@scenario("monkey", weight=8, target=YP3)
def monkey(t: Yp3Torture) -> str:
    """60 random keys anywhere over music, then back out"""
    t.music()
    h = t.health()
    script = " ".join(no_folder_skip(t.rng.choices(MONKEY_KEYS, MONKEY_WEIGHTS, k=60)))
    t.keys(script)
    t.keys("L L L L L")
    t.check_health(h)
    return script


@scenario("plugins", weight=6, target=YP3)
def plugins(t: Yp3Torture) -> str | None:
    """A random plugin over music, keys, then out with the YP3's keys (no
    health check: plugins take the CPU)"""
    t.music()
    cat = t.rng.choice(PLUGIN_DIRS)
    rocks = [r for r in ls_entries(t.sh(f"ls {ROCKS}/{cat}"))
             if r.endswith(".rock") and r not in PLUGIN_SKIP]
    if not rocks:
        return f"skipped: no plugins in {ROCKS}/{cat}"
    p = t.rng.choice(rocks)
    t.sh(f"run {ROCKS}/{cat}/{p}")
    time.sleep(3)
    t.keys(" ".join(t.rng.choices(["U", "D", "L", "R", "S", "200"], k=20)))
    if not t.close_plugin():
        t.fail("did not exit (no way out with the YP3's keys: a keymap bug)")
        t.boot()
    return p


@scenario("menus", weight=6, target=YP3)
def menus(t: Yp3Torture) -> str:
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


@scenario("dbdeep", weight=4, target=YP3)
def dbdeep(t: Yp3Torture) -> str | None:
    """Deep into the database, Home, Database again: the next visit must
    start at the root (a stale table once spun the UI until the watchdog)"""
    t.music()
    h = t.health()
    act = t.menu("database")
    if act != ACT_DATABASE:
        # low memory: the menus come from free heap only
        if any("the database" in text for kind, text in t.msgs() if kind == "yesno"):
            t.home()
            return f"refused: not enough memory while music plays (free {h.num('free')})"
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


@scenario("db", weight=4, target=YP3)
def db(t: Yp3Torture) -> str:
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


@scenario("crawl", weight=16, target=YP3)
def crawl(t: Yp3Torture) -> str | None:
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
        t.sh(f"cfgsave {CFG_DIR}/t_base.cfg")
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
    t.sh(f"cfg {CFG_DIR}/t_base.cfg")      # undo settings the keys changed
    time.sleep(3)
    return what


@scenario("sleep", weight=4, target=YP3)
def sleep(t: Yp3Torture) -> str:
    """LCD sleep (the controller's clock gated) and wake by a key, sometimes
    with settings loaded while asleep"""
    t.music()
    h = t.health()
    t.sh(f"cfg {CFG_DIR}/t_sleep.cfg")
    time.sleep(9)
    asleep = not t.gate_on()
    again = t.rng.random() < 0.5
    if again:
        t.sh(f"cfg {CFG_DIR}/t_sleep.cfg")      # settings applied while asleep
        time.sleep(1)
    t.keys("D 1500")
    awake = t.gate_on()
    t.sh(f"cfg {CFG_DIR}/t_wake.cfg")
    if not asleep or not awake:
        t.fail(f"clock gate: asleep {asleep}, awake {awake}")
    t.check_health(h)
    return "sleep/wake" + (", settings loaded asleep" if again else "")


# ---------------------------------------------------------------- recording and radio

@scenario("record", weight=8, target=YP3)
def record(t: Yp3Torture) -> str:
    """dev `rec`: a random format, source and channels, sometimes MP3
    pre-recording; no pcmrec warnings, the time asked recorded, the file
    readable. A refusal for want of heap (~93 KB) is a note."""
    fmt = t.rng.choice(["wav", "aiff", "wv", "mp3"])
    src = t.rng.choice(["fm", "fm", "mic"])
    chans = "mono" if src == "mic" or t.rng.random() < 0.3 else "stereo"
    secs = t.rng.randint(5, 25)
    pre = t.rng.choice([0, 0, 2, 3]) if fmt == "mp3" else 0
    what = f"{secs}s {src} {chans} {fmt} pre {pre}"
    t.home()                            # not from a Recording screen monkey left recording
    t.sh("stop")
    if src == "fm":
        t.sh(f"fm {t.rng.choice(FM_KHZ)}")
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
        meta = " ".join(t.sh(f"meta {CFG_DIR}/t_rec.{fmt}", timeout=60))
        m = re.search(r"meta: 1 in \d+ ms: length (\d+) ms", meta)
        if not m or abs(int(m[1]) - rec[0]) > 1500:
            t.fail(f"file: {meta[:120]}")
        what += f", {rec[0]} ms"
    t.sh(f"rm {CFG_DIR}/t_rec.{fmt}")
    if src == "fm":
        t.sh("fm off")
    t.check_health(h)
    return what


@scenario("recui", weight=4, target=YP3)
def recui(t: Yp3Torture) -> str | None:
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


@scenario("fmui", weight=4, target=YP3)
def fmui(t: Yp3Torture) -> str | None:
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


@scenario("fm", weight=6, target=YP3)
def fm(t: Yp3Torture) -> str:
    """Tune, read the tuner over I2C, run the FM monitor a while: the capture
    keeps running without new overruns (but on a Bluetooth headset, no real
    use: the radio needs wired headphones as its antenna)"""
    khz = t.rng.choice(FM_KHZ)
    t.sh("stop")                        # as the radio screen does
    h = t.health()
    t.sh(f"fm {khz}")
    regs = " ".join(t.sh("fmregs"))
    m0 = " ".join(t.sh("fmmon"))
    time.sleep(t.rng.uniform(5, 20))
    m1 = " ".join(t.sh("fmmon"))
    t.sh("fm off")
    p0, p1 = fm_periods(m0), fm_periods(m1)
    if "r=-1" in regs or not regs:
        t.fail(f"fmregs: {regs[:100]}")
    elif not p0 or not p1 or p1[0] <= p0[0]:
        t.fail(f"capture not running: {m1[:100]}")
    elif p1[1] > p0[1]:
        bt = parse_bt_status(t.sh("bt stack status"))
        if not (bt and bt.sink == SINK_BT):
            t.fail(f"capture overruns +{p1[1] - p0[1]}" + fm_consumer(m0, m1))
    t.check_health(h)
    return f"{khz} kHz"


# ---------------------------------------------------------------- the card and USB

@scenario("fs", weight=4, target=YP3)
def fs(t: Yp3Torture) -> str:
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


@scenario("sdodd", weight=4, target=YP3)
def sdodd(t: Yp3Torture) -> str:
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


@scenario("meta", weight=4, target=YP3)
def meta(t: Yp3Torture) -> str | None:
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


@scenario("usb", weight=2, target=YP3)
def usb(t: Yp3Torture) -> str:
    """A simulated unplug and replug during playback"""
    h = t.health()
    t.sh(f"play {CODECTEST}/mp3_320.mp3")
    t.usbcycle()
    t.check_health(h)
    return "usbcycle during playback"


# ---------------------------------------------------------------- Bluetooth

def bt_scenario(case: str, weight: int) -> Callable[[Callable[[Yp3Torture], str]],
                                                     Callable[[Yp3Torture], str]]:
    """A bt-CASE step (Yp3Torture.bt_case around FN)"""
    def register(fn: Callable[[Yp3Torture], str]) -> Callable[[Yp3Torture], str]:
        def step(t: Yp3Torture) -> str:
            return t.bt_case(fn)
        step.__doc__ = fn.__doc__
        scenario(f"bt-{case}", weight=weight, target=YP3, needs={BT_PC})(step)
        return fn
    return register


@bt_scenario("onoff", 3)
def bt_onoff(t: Yp3Torture) -> str:
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
def bt_pcdisc(t: Yp3Torture) -> str:
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
def bt_pcpower(t: Yp3Torture) -> str:
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
def bt_avrcp(t: Yp3Torture) -> str:
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
def bt_churn(t: Yp3Torture) -> str:
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
    if track := t.library_track():      # test files are short
        t.sh(f"play {track}")
        time.sleep(2)
    t.bt_streaming(what)
    return what


@bt_scenario("disturb", 2)
def bt_disturb(t: Yp3Torture) -> str:
    """A theme switch, a USB cycle, a plugin, browsing, an inquiry or LCD
    sleep while streaming: still linked, streaming"""
    disturbance = t.rng.choice(["theme", "usb", "plugin", "browse", "scan", "sleep"])
    what = f"disturb: {disturbance}"
    if disturbance == "theme":          # stops playback, as upstream
        cfg = t.rng.choice(THEME_CFGS)
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
        t.sh(f"cfg {CFG_DIR}/t_sleep.cfg")
        time.sleep(9)
        t.keys("D 1500")
        t.sh(f"cfg {CFG_DIR}/t_wake.cfg")
    s, secs = t.bt_wait(t.bt_up, BT_CONNECT_S)     # the plugin may have needed a boot
    if secs is None:
        t.fail(f"{what}: link lost: status {s.status}")
    else:
        t.bt_streaming(what)
    return what


@bt_scenario("reboot", 1)
def bt_reboot(t: Yp3Torture) -> str:
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


# ---------------------------------------------------------------- commands

@command("crawl-coverage", help="the torture crawl's graph of the UI: coverage, or Graphviz dot",
         target=YP3, args=[arg("--graph", action="store_true", help="the graph as Graphviz dot")])
def crawl_coverage(ctx: Context, args: Namespace) -> int:
    """From [targets.yp3] menu_coverage (default <paths.build>/menu-coverage.json)"""
    cov = Coverage(coverage_file(ctx.t))
    print("\n".join(cov.dot() if args.graph else cov.summary()))
    return 0
