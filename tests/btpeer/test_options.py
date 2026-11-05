"""A sink run's timed steps and the HCI server address."""

from __future__ import annotations

from pathlib import Path

from auto_rockbox.btpeer.options import HidOptions, Step, parse_steps, split_hci
from auto_rockbox.errors import UsageError


def _refused(text: str) -> str:
    try:
        parse_steps(text)
    except UsageError as e:
        return str(e)
    raise AssertionError(f"{text!r} was accepted")


def test_steps() -> None:
    steps = parse_steps(" 5:pause 7:PLAY,9:vol=40  12:lose 13:rate=300 14:nodiscover=2 ")
    assert steps == [Step(5, "pause"), Step(7, "play"), Step(9, "vol", 40), Step(12, "lose"),
                     Step(13, "rate", 300), Step(14, "nodiscover", 2)]
    assert [str(s) for s in steps[2:4]] == ["vol=40", "lose"]
    assert parse_steps("") == []
    assert parse_steps("0.5:batt=100")[0] == Step(0.5, "batt", 100)


def test_bad_steps() -> None:
    assert "T:KEY" in _refused("x:play")
    assert "vol is at most 127" in _refused("1:vol=128")
    assert "batt is at most 100" in _refused("1:batt=101")
    assert "unknown key 'eject'" in _refused("1:eject")
    assert "unknown key 'vol'" in _refused("1:vol")
    assert "unknown key 'play=1'" in _refused("1:play=1")


def test_split_hci() -> None:
    assert split_hci("127.0.0.1:6402") == ("127.0.0.1", 6402)
    assert split_hci("_:0") == (None, 0)
    for bad in ("6402", "host:", "host:port"):
        try:
            split_hci(bad)
        except UsageError:
            continue
        raise AssertionError(f"{bad!r} was accepted")


def test_hid_report_rate() -> None:
    out = Path("out")
    assert HidOptions(out).reports_per_s == 1000.0              # a DS4 streams
    assert HidOptions(out, kind="keyboard").reports_per_s == 0.0    # on change only
    assert HidOptions(out, rate=250).reports_per_s == 250
