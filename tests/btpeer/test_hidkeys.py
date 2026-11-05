"""The HID peer's reports, the DS4 output report, and the checks of what the
simulator and a player made of the presses."""

from __future__ import annotations

import zlib

from auto_rockbox.btpeer import hidkeys
from auto_rockbox.btpeer.hidkeys import SimButtons


def test_keys_for() -> None:
    assert "x" not in hidkeys.keys_for("keyboard")
    assert {"x", "y", "l2", "r2", "sup", "sleft"} <= set(hidkeys.keys_for("ds4"))
    assert set(hidkeys.keys_for("keyboard")) <= set(hidkeys.keys_for("ds4"))


def test_keyboard_report() -> None:
    assert hidkeys.keyboard_report([]) == bytes([1, 0, 0, 0, 0, 0, 0, 0, 0])
    assert hidkeys.keyboard_report(["up", "a"]) == bytes([1, 0, 0, 0x28, 0x52, 0, 0, 0, 0])
    many = hidkeys.keyboard_report(hidkeys.keys_for("keyboard"))
    assert len(many) == 9 and all(many[3:])       # six usages at most


def test_ds4_report() -> None:
    idle = hidkeys.ds4_report([], 0, extended=False)
    assert idle == bytes([0x01, 0x80, 0x80, 0x80, 0x80, 0x08, 0, 0, 0, 0])
    r = hidkeys.ds4_report(["right", "a", "home", "sup"], 5, extended=False)
    assert r[2] == 0 and r[5] == 0x20 | 2 and r[7] == 0x01 | 5 << 2
    x = hidkeys.ds4_report(["right", "a", "home", "sup"], 5, extended=True)
    assert len(x) == hidkeys.DS4_EXTENDED_LEN
    assert x[:3] == bytes([0x11, 0xC0, 0x00]) and x[3:12] == r[1:]


def _output(control: int, flags: int, light: tuple[int, int, int]) -> bytes:
    pdu = bytearray(75)
    pdu[:2] = b"\xa2\x11"
    pdu[2], pdu[4] = control, flags
    pdu[9:12] = bytes(light)
    return bytes(pdu) + zlib.crc32(pdu).to_bytes(4, "little")


def test_ds4_output() -> None:
    out = hidkeys.ds4_output(_output(0xC4, 0xF7, (0, 0, 64)))
    assert out == hidkeys.Ds4Output(0xC4, 0xF7, (0, 0, 64))
    assert isinstance(out, hidkeys.Ds4Output) and out.interval_ms == 4
    bad = bytearray(_output(0xC4, 0xF7, (0, 0, 64)))
    bad[10] ^= 1
    assert isinstance(hidkeys.ds4_output(bytes(bad)), str)
    assert isinstance(hidkeys.ds4_output(b"\xa2\x11\x00"), str)
    assert hidkeys.ds4_output(b"\xa2\x01" + bytes(77)) is None      # another report


def test_stack_bits_and_sim_buttons() -> None:
    assert [hidkeys.stack_bits(k) for k in ("up", "down", "left", "right")] == [1, 2, 4, 8]
    assert hidkeys.stack_bits("sleft") == 4
    assert hidkeys.stack_bits("a") == 0x10 and hidkeys.stack_bits("home") == 0x10 << 10
    b = SimButtons()
    assert b.held("sdown") == 2 and b.held("a") == 1 << 13 and b.held("b") == 2 << 13
    other = SimButtons(up=0x100, down=0x200, left=0x400, right=0x800, pad_shift=20)
    assert other.held("up") == 0x100 and other.held("x") == 4 << 20


SIM_LOG = """\
bt-test: pairing with F0:F0:F0:F0:F0:03
bt: hid ds4 connected
bt: hid keys 001
bt-test: buttons 0001
bt: hid keys 000
bt-test: buttons 0000
bt: hid keys 010
bt-test: buttons 2000
bt-test: buttons 0000
"""


def test_check_sim_log() -> None:
    ok = hidkeys.check_sim_log(SIM_LOG, ["up", "a"], SimButtons())
    assert ok.ok and ok.seen == [1, 0x2000]
    assert "stack key states: 001 000 010" in ok.notes
    assert "sim: bt: hid ds4 connected" in ok.notes
    assert "sim: bt-test: pairing with F0:F0:F0:F0:F0:03" in ok.notes
    missed = hidkeys.check_sim_log(SIM_LOG, ["up", "a", "b"], SimButtons())
    assert not missed.ok and missed.want == [1, 0x2000, 0x4000]


def test_check_player_log() -> None:
    lines = ["bt: hid keys 004",                # an earlier connection's
             "bt: hid keyboard connected", "bt: hid keys 001", "bt: hid keys 000",
             "bt: hid keys 020", "bt: hid keys 000", "ok"]
    out = hidkeys.check_player_log(lines, ["up", "b"])
    assert out.ok and out.seen == [1, 0x20]
    assert out.notes[0] == "player key states: 001 020"
    assert not hidkeys.check_player_log(lines, ["up"]).ok
    assert not hidkeys.check_player_log(["ok"], ["up"]).ok
