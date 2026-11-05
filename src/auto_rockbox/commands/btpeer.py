"""`arb bt-peer`: test peers for Rockbox's own Bluetooth stack
(auto_rockbox.btpeer): a virtual A2DP sink or HID device against the
simulator, and this PC's adapter as a sink or a headset for a player; `arb
yp3 bt-peer hid`: the adapter as an input device for a player on its dev
shell.

    [bluetooth]
    player = "AA:BB:CC:DD:EE:FF"    # the player's BD_ADDR (pcheadset, yp3 bt-peer hid)
    adapter = "usb:VID:PID"         # this PC's adapter as a Bumble transport (pcheadset,
                                    # yp3 bt-peer hid); Bumble takes it from BlueZ
    sim_hci = "127.0.0.1:6402"      # where the virtual controller serves the simulator
    capture_sink = "arb_bt_capture" # the null sink pcsink makes, records and removes

    [bluetooth.sim_buttons]         # the simulator target's button-target.h (hid --sim);
    up = 1                          # these are the defaults
    down = 2
    left = 4
    right = 8
    pad_shift = 13                  # BUTTON_PAD_SHIFT

The Bumble peers need the optional dependency (`uv sync --extra bt`);
pcsink does not.
"""

from __future__ import annotations

import argparse
from argparse import Namespace
from collections.abc import Callable
from dataclasses import fields
from pathlib import Path

from ..btpeer import need_bumble
from ..btpeer.hidkeys import KINDS, SimButtons, keys_for
from ..btpeer.options import (
    HCI,
    HID_ADDRESS,
    SINK_ADDRESS,
    STACK_ADDRESS,
    HeadsetOptions,
    HidOptions,
    PcSinkOptions,
    SinkOptions,
    StackSettings,
    parse_steps,
)
from ..errors import UsageError
from ..registry import Arg, Context, arg, command, group
from ..workspace import ConfigError


def _bounded(lo: int, hi: int) -> Callable[[str], int]:
    def check(text: str) -> int:
        v = int(text)
        if not lo <= v <= hi:
            raise argparse.ArgumentTypeError(f"{v} is not in {lo}..{hi}")
        return v
    check.__name__ = f"integer {lo}..{hi}"
    return check


def _setting(ctx: Context, given: str | None, key: str, what: str, flag: str) -> str:
    value = given or ctx.ws.section("bluetooth").get(key)
    if not isinstance(value, str):
        raise ConfigError(f"no {flag} given and no [bluetooth] {key} ({what})")
    return value


def _hci(ctx: Context, given: str | None) -> str:
    return given or ctx.ws.section("bluetooth").string("sim_hci", HCI)


def _out(ctx: Context, given: Path | None, name: str) -> Path:
    return given or ctx.ws.build_dir / name


def _presses(kind: str, text: str) -> list[str]:
    keys = text.split()
    have = keys_for(kind)
    bad = [k for k in keys if k not in have]
    if bad:
        raise UsageError(f"{kind} has no key {', '.join(bad)} (it has {' '.join(have)})")
    return keys


def _sim_buttons(ctx: Context) -> SimButtons:
    s = ctx.ws.section("bluetooth", "sim_buttons")
    d = SimButtons()
    return SimButtons(**{f.name: s.integer(f.name, getattr(d, f.name)) for f in fields(d)})


def sink_cmd(ctx: Context, args: Namespace) -> int:
    """A virtual A2DP sink with an AVRCP controller (and, with --hf, an HFP
    hands-free unit) against the stack under test in the simulator: two
    Bumble controllers on one link, A served to the simulator as H4 over
    TCP ($ROCKBOX_BT_HCI), the sink on B. Strict as a real headset; it
    judges the stream (tone, dropouts, stalls, rate, RTP/SBC problems,
    stack violations) after --seconds of audio. Exit 0: PASS. Start the
    simulator once it is up, e.g. in its build directory:

        SDL_VIDEODRIVER=offscreen SDL_AUDIODRIVER=dummy ROCKBOX_BT_HCI=127.0.0.1:6402 \\
          ROCKBOX_BT_TEST="connect=F0:F0:F0:F0:F0:02 play=/tone.mp3 off=72" ./rockboxui

    Outputs in OUT: peer.log, stream.sbc, stream.wav, keys.json,
    hci-stack.btsnoop."""
    need_bumble()
    from ..btpeer import sink  # noqa: PLC0415
    opts = SinkOptions(
        out=_out(ctx, args.out, "bt-peer"), hci=_hci(ctx, args.hci),
        stack=StackSettings(args.stack_address, args.acl_size, args.acl_count, args.acl_rate,
                            eir=not args.no_eir),
        sink_address=args.sink_address, mtu=args.mtu, connect=args.connect,
        connect_delay=args.connect_delay, connect_timeout=args.connect_timeout,
        sink_central=args.sink_central, sink_no_switch=args.sink_no_switch,
        configure=args.configure, keys=parse_steps(args.keys or ""),
        abs_volume=args.abs_volume, suspend=args.suspend, disconnect=args.disconnect,
        hf=args.hf, hf_apple=args.hf_apple, hf_connect=args.hf_connect,
        drop_discover=args.drop_discover, drop_avdtp_open=args.drop_avdtp_open,
        drop_avrcp=args.drop_avrcp, avrcp_query=args.avrcp_query, seconds=args.seconds,
        expect_freq=args.expect_freq, exit_after=args.exit_after, sdp=args.sdp,
        allow_unencrypted=args.allow_unencrypted, forget=args.forget, verbose=args.verbose,
        trace=args.trace)
    return sink.main(opts)


def _hid_options(ctx: Context, args: Namespace) -> HidOptions:
    """The options both HID commands have"""
    return HidOptions(
        out=_out(ctx, args.out, "bt-hidpeer"), kind=args.kind,
        press=_presses(args.kind, args.press), connect=args.connect,
        connect_delay=args.connect_delay, step=args.step, hold=args.hold, rate=args.rate,
        refuse_boot=args.refuse_boot, legacy=args.legacy, type_delay=args.type_delay,
        just_works=args.just_works, silent=args.silent, unplug=args.unplug,
        timeout=args.timeout, forget=args.forget, verbose=args.verbose)


def hid_cmd(ctx: Context, args: Namespace) -> int:
    """A virtual DualShock 4 or keyboard against the stack's HID host in the
    simulator (controller B; controller A served as H4 over TCP). Without
    --connect the stack scans and connects to it; with --connect it pages
    the stack, as a paired device does. Once both HID channels are up it
    presses --press, one key each --step s. With --sim DIR it starts the
    simulator there ($ROCKBOX_BT_TEST "on=2 scan connect=DEVICE"; its log in
    OUT/sim.log) and compares the buttons the simulator holds with the
    presses ([bluetooth.sim_buttons]). Exit 0: PASS. Outputs in OUT:
    peer.log, sim.log, keys.json, hci-stack.btsnoop, shotNN.png (--shots)."""
    need_bumble()
    from ..btpeer import hid  # noqa: PLC0415
    opts = _hid_options(ctx, args)
    opts.hci = _hci(ctx, args.hci)
    opts.stack = StackSettings(args.stack_address)
    opts.device_address, opts.sim, opts.shots = args.device_address, args.sim, args.shots
    opts.buttons = _sim_buttons(ctx)
    opts.trace = args.trace
    return hid.main(opts)


def hid_air_cmd(ctx: Context, args: Namespace) -> int:
    """This PC's adapter (Bumble over raw USB; BlueZ loses it meanwhile, so
    nothing may be connected to BlueZ) as a DualShock 4 or keyboard for the
    player, which runs a dev build with its USB shell. The shell turns
    Bluetooth on, scans and connects to the adapter (with --connect the
    adapter pages the player); the presses are checked against the key
    states the player's log shows ("bt: hid keys"). --stay S: then S s more
    connected, the link and the player's health polled; --play FILE: the
    player plays FILE meanwhile. Exit 0: PASS. Outputs in OUT: peer.log,
    keys.json, hci-adapter.btsnoop."""
    need_bumble()
    from ..btpeer import hid  # noqa: PLC0415
    adapter = _setting(ctx, args.adapter, "adapter", "a Bumble transport, usb:VID:PID",
                       "--adapter")
    player = _setting(ctx, args.addr, "player", "its BD_ADDR", "--addr")
    opts = _hid_options(ctx, args)
    opts.stay, opts.play = args.stay, args.play
    return hid.main_air(opts, ctx.t.shell(), adapter, player)


def pcsink_cmd(ctx: Context, args: Namespace) -> int:
    """This PC's adapter as the player's A2DP sink, through BlueZ: a
    bluetoothctl agent makes it discoverable and pairable, answers its
    prompts with yes and trusts the device named --name...; a null sink is
    the default sink meanwhile (so nothing plays on the speakers). Once the
    player's stream appears, --seconds s of it are recorded and analysed:
    level, tone and purity per second, dropouts. On the player: Bluetooth
    on, connect to the adapter address this prints, the Bluetooth sink,
    then play a tone. Outputs in OUT: bluetoothctl.log, stream.wav."""
    from ..btpeer import pcsink  # noqa: PLC0415
    capture = args.capture_sink or ctx.ws.section("bluetooth").string("capture_sink",
                                                                      "arb_bt_capture")
    pcsink.main(PcSinkOptions(out=_out(ctx, args.out, "bt-pcsink"), seconds=args.seconds,
                              wait=args.wait, name=args.name, agent=args.agent,
                              capture_sink=capture))
    return 0


def _range(text: str) -> tuple[float, float]:
    a, _, b = text.partition(":")
    try:
        return float(a), float(b or a)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r}: S or A:B") from None


def pcheadset_cmd(ctx: Context, args: Namespace) -> int:
    """This PC's adapter (Bumble over raw USB; BlueZ loses it meanwhile) as
    a headset that pages the player cycle after cycle, as a Sony WH-1000XM3
    does at each power-on: page, authenticate (Just Works the first time),
    encrypt, ask for the peripheral role --switch-after s after the page,
    query SDP, leave AVDTP to the player; a cycle passes once --play s of
    media arrived. With --lose-share the headset vanishes once the music
    plays (no detach: a supervision timeout at the player) and waits for the
    player's page; --relose-share of those pages vanish again before the
    stream. On the player: Bluetooth on and music playing to the Bluetooth
    sink. OUT/cycles.txt has one line per cycle (result, stage:seconds,
    notes); the exit status does not judge them. Also OUT/headset.log,
    hci.btsnoop, keys.json."""
    need_bumble()
    from ..btpeer import pcheadset  # noqa: PLC0415
    opts = HeadsetOptions(
        out=_out(ctx, args.out, "bt-pcheadset"),
        adapter=_setting(ctx, args.adapter, "adapter", "a Bumble transport, usb:VID:PID",
                         "--adapter"),
        player=_setting(ctx, args.addr, "player", "its BD_ADDR", "--addr"),
        name=args.name, cycles=args.cycles, switch_after=args.switch_after,
        switch_share=args.switch_share, deadline=args.deadline, min_packets=args.min_packets,
        play=args.play, gap=args.gap, repage_wait=args.repage_wait,
        lose_share=args.lose_share, relose_share=args.relose_share, seed=args.seed)
    pcheadset.main(opts)
    return 0


# ---------------------------------------------------------------- arguments

def _out_arg(name: str) -> Arg:
    return arg("--out", type=Path, help=f"output directory (default <paths.build>/{name})")


_HCI = arg("--hci", metavar="HOST:PORT", help="the H4 server the simulator connects to "
           f"(HOST _: every interface; default [bluetooth] sim_hci, {HCI})")
_STACK = arg("--stack-address", default=STACK_ADDRESS,
             help=f"controller A's address, the stack's (default {STACK_ADDRESS})")
_LOGGING = [arg("-v", "--verbose", action="store_true", help="Bumble's debug log too"),
            arg("--trace", action="store_true", help="log controller A's HCI traffic")]
_FORGET = arg("--forget", action="store_true", help="delete the stored link keys first "
              "(hid over the air: the player forgets the adapter too)")
_ADAPTER = arg("--adapter", help="this PC's adapter, a Bumble transport such as usb:VID:PID "
               "(default [bluetooth] adapter)")
_ADDR = arg("--addr", help="the player's BD_ADDR (default [bluetooth] player)")

_HID_COMMON = [
    arg("--kind", choices=sorted(KINDS), default="ds4", help="the device (default ds4)"),
    arg("--press", default="", metavar='"KEY ..."',
        help="keys pressed once both channels are up: up down left right a b l1 r1 menu "
        "view home; a ds4 also x y l2 r2 and sup sdown sleft sright (its left stick)"),
    arg("--connect", action="store_true", help="the device pages the stack and opens its "
        "channels, as a paired one does (pair without it first)"),
    arg("--connect-delay", type=float, default=5.0, metavar="S"),
    arg("--step", type=float, default=1.0, metavar="S", help="between presses (default 1)"),
    arg("--hold", type=float, default=0.15, metavar="S", help="each press (default 0.15: "
        "under the 0.3 s key repeat, so a press moves a menu one step)"),
    arg("--rate", type=float, metavar="N", help="ds4 reports per second until the host sets "
        "the interval (default 1000; a keyboard sends on change only)"),
    arg("--refuse-boot", action="store_true", help="keyboard: refuse SET_PROTOCOL (boot) but "
        "send the boot layout anyway, as cheap keyboards do"),
    arg("--legacy", action="store_true", help="keyboard: no Secure Simple Pairing: it types "
        "the PIN the stack shows (over the air only)"),
    arg("--type-delay", type=float, default=0.0, metavar="S",
        help="keyboard: type the code S s after the stack shows it"),
    arg("--just-works", action="store_true",
        help="keyboard: NoInputNoOutput, so no passkey (some keyboards are)"),
    arg("--silent", action="store_true",
        help="with --connect: open no channels (the stack should drop the link)"),
    arg("--unplug", action="store_true",
        help="send a virtual cable unplug at the end (the stack should forget the device)"),
    arg("--timeout", type=float, default=40.0, metavar="S",
        help="for the HID channels to come up (default 40)"),
    _FORGET, _out_arg("bt-hidpeer"),
]

group("bt-peer", help="Bluetooth test peers: virtual sink or HID device against the "
      "simulator, this PC's adapter as a sink or headset for a player")

command("bt-peer sink", target=False, help="virtual A2DP sink + AVRCP controller against "
        "the simulator", args=[
    _HCI, _STACK,
    arg("--sink-address", default=SINK_ADDRESS, help=f"the sink's (default {SINK_ADDRESS})"),
    arg("--acl-size", type=int, default=1021, help="controller A's ACL buffer size"),
    arg("--acl-count", type=int, default=8, help="controller A's ACL buffer count"),
    arg("--acl-rate", type=int, default=1000, metavar="KBIT", help="air rate that frees "
        "controller A's ACL buffers (0: at once; default 1000)"),
    arg("--mtu", type=int, default=672, help="the sink's L2CAP MTU for AVDTP channels"),
    arg("--no-eir", action="store_true", help="controller A refuses Write_Inquiry_Mode(2): "
        "no EIR, the stack must ask for names"),
    arg("--connect", action="store_true", help="the sink pages the stack"),
    arg("--connect-delay", type=float, default=3.0, metavar="S"),
    arg("--connect-timeout", type=float, default=60.0, metavar="S"),
    arg("--sink-central", action="store_true",
        help="accept the stack's page as central, as BlueZ does"),
    arg("--sink-no-switch", action="store_true",
        help="--connect without allowing a role switch"),
    arg("--configure", action="store_true",
        help="with --connect: the sink configures and opens the stream"),
    arg("--keys", metavar='"T:KEY ..."', help="timed steps, T s after the stream starts: "
        "AVRCP keys (play pause stop next previous volup voldown ffwd rewind), vol=N, "
        "batt=N, rate=KBIT, nodiscover=N, noopen=N, lose"),
    arg("--abs-volume", type=_bounded(0, 127), metavar="V",
        help="the sink has AVRCP absolute volume, starting at V (0..127)"),
    arg("--suspend", type=float, metavar="T",
        help="the sink sends AVDTP SUSPEND T s after the stream starts"),
    arg("--disconnect", type=float, metavar="T",
        help="the sink disconnects T s after the stream starts"),
    arg("--hf", type=_bounded(0, 100), metavar="PERCENT",
        help="the sink is also an HFP hands-free unit reporting this battery level"),
    arg("--hf-apple", action="store_true",
        help="with --hf: report the battery with AT+XAPL/AT+IPHONEACCEV"),
    arg("--hf-connect", action="store_true",
        help="with --hf: the sink opens the HFP link once the stream is open"),
    arg("--drop-discover", type=int, default=0, metavar="N",
        help="leave the stack's first N AVDTP DISCOVERs unanswered"),
    arg("--drop-avdtp-open", type=int, default=0, metavar="N",
        help="leave the stack's first N AVDTP L2CAP connection requests unanswered"),
    arg("--drop-avrcp", type=int, default=0, metavar="N",
        help="leave the stack's first N AVRCP commands unanswered"),
    arg("--avrcp-query", action="store_true",
        help="also send GetCapabilities, GetPlayStatus and RegisterNotification"),
    arg("--seconds", type=float, default=60.0, help="audio to receive (default 60)"),
    arg("--expect-freq", type=float, default=1000.0, metavar="HZ",
        help="the tone the stream should carry (default 1000)"),
    arg("--exit-after", action=argparse.BooleanOptionalAction, default=True,
        help="exit after the report (--no-exit-after: keep serving)"),
    arg("--sdp", action="store_true", help="log the stack's SDP records"),
    arg("--allow-unencrypted", action="store_true",
        help="accept AVDTP and AVCTP on an unencrypted link"),
    _FORGET, _out_arg("bt-peer"), *_LOGGING,
])(sink_cmd)

command("bt-peer hid", target=False, help="virtual DualShock 4 or keyboard against the "
        "simulator", args=[
    *_HID_COMMON, _HCI, _STACK,
    arg("--device-address", default=HID_ADDRESS, help=f"the device's (default {HID_ADDRESS})"),
    arg("--sim", type=Path, metavar="DIR", help="start the simulator in this build directory "
        "and check the buttons it logs (without it, start it yourself)"),
    arg("--shots", action="store_true",
        help="with --sim: a screenshot after each release (OUT/shotNN.png)"),
    *_LOGGING,
])(hid_cmd)

command("bt-peer hid", target={"yp3"}, needs={"shell"},
        help="this PC's adapter as a DualShock 4 or keyboard for the player", args=[
    *_HID_COMMON, _ADAPTER, _ADDR,
    arg("--stay", type=float, default=0.0, metavar="S",
        help="stay connected S s more after the presses, polling the link and health"),
    arg("--play", metavar="FILE", help="the player plays FILE meanwhile"),
    arg("-v", "--verbose", action="store_true", help="Bumble's debug log too"),
])(hid_air_cmd)

command("bt-peer pcsink", target=False, help="this PC as the player's A2DP sink (BlueZ); "
        "analyse the stream", args=[
    arg("--seconds", type=int, default=30, help="to record (default 30)"),
    arg("--wait", type=int, default=600, metavar="S",
        help="for the player's stream to appear (default 600)"),
    arg("--name", default="Rockbox", help="trust devices whose name starts with this "
        "(default Rockbox)"),
    arg("--agent", default="NoInputNoOutput",
        help="the bluetoothctl agent's capability (default NoInputNoOutput)"),
    arg("--capture-sink", metavar="NAME",
        help="the null sink to make and record (default [bluetooth] capture_sink, "
        "arb_bt_capture)"),
    _out_arg("bt-pcsink"),
])(pcsink_cmd)

command("bt-peer pcheadset", target=False, help="this PC's adapter as a headset paging the "
        "player, cycle after cycle", args=[
    _ADAPTER, _ADDR,
    arg("--cycles", type=int, default=1),
    arg("--name", default="Test Headset", help="the headset's name (default Test Headset)"),
    arg("--switch-after", type=_range, default=(3.0, 3.0), metavar="S|A:B",
        help="ask for the peripheral role S s (or a time in A..B) after the page (default 3)"),
    arg("--switch-share", type=float, default=1.0,
        help="share of cycles that switch (default 1)"),
    arg("--no-switch", dest="switch_share", action="store_const", const=0.0,
        help="no cycle switches"),
    arg("--deadline", type=float, default=45.0, metavar="S",
        help="for every stage, from the page (default 45)"),
    arg("--min-packets", type=int, default=20,
        help="media packets that make the music play (default 20)"),
    arg("--play", type=float, default=5.0, metavar="S",
        help="media to receive once it plays (default 5)"),
    arg("--gap", type=float, default=5.0, metavar="S", help="between cycles (default 5)"),
    arg("--repage-wait", type=float, default=25.0, metavar="S",
        help="for the player's page after a lost link (default 25)"),
    arg("--lose-share", type=float, default=0.0,
        help="share of cycles whose link vanishes once the music plays"),
    arg("--relose-share", type=float, default=0.0,
        help="share of the player's re-pages that vanish before the stream"),
    arg("--seed", type=int, help="random seed (switch times, losses)"),
    _out_arg("bt-pcheadset"),
])(pcheadset_cmd)
