"""Key scripts: what a dev build presses by itself after boot (run config
keys=, shell `keys`; the firmware's dev_keys(), firmware/devlog.c).

    "9000 D D S 1500 UL P+++"

Tokens are separated by spaces or commas. A number waits that many ms. A run
of key letters presses those keys together (a chord) for a 150 ms tap; a
suffix changes the press:
    X+     held 1 s, one more second per further +  (X++ = 2 s)
    X*     held 1.2 s: a long press
    X~     held 1/8 s: a short tap
On a touchscreen (the target's "touch" capability), @X:Y touches the panel
there and @X:Y>X2:Y2 drags from one point to the other over the press, in
the panel's coordinates: "@120:300>120:40" flicks up in 150 ms,
"@120:300>120:40+" drags for a second; the suffixes are the keys'.
200 ms released follow every press. Every press goes through the player's
button driver, as a user's would.

Letters are the target's (`Target.key_letters`); the firmware parses the
script, the host only checks it so that a typo fails before a flash rather
than after a boot.
"""

from __future__ import annotations

import re

from .errors import ArbError

SUFFIXES = "+*~"
TAP_MS = 150
HOLD_MS = {"+": 1000, "*": 1200, "~": 125}   # "+" per +
RELEASE_MS = 200


class KeyScriptError(ArbError, ValueError):
    pass


def tokens(script: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", script.strip()) if t]


def check_script(script: str, letters: str, suffixes: str = SUFFIXES,
                 panel: tuple[int, int] | None = None) -> None:
    """Raise KeyScriptError on a token the firmware would refuse. PANEL: the
    touch panel's (width, height), when touch tokens are allowed"""
    suffix = rf"(\++|[{re.escape(suffixes)}])?"
    key = re.compile(rf"\d+|[{re.escape(letters)}]+{suffix}")
    touch = re.compile(rf"@(\d+):(\d+)(?:>(\d+):(\d+))?{suffix}")
    for t in tokens(script):
        if key.fullmatch(t):
            continue
        m = touch.fullmatch(t)
        if not m:
            raise KeyScriptError(f"bad key-script token {t!r} (letters {letters}, "
                                 f"suffixes {suffixes})")
        if panel is None:
            raise KeyScriptError(f"touch token {t!r} on a target without a touchscreen")
        coords = [int(g) for g in m.groups()[:4] if g is not None]
        if any(c >= panel[i % 2] for i, c in enumerate(coords)):
            raise KeyScriptError(f"touch token {t!r} outside the {panel[0]}x{panel[1]} panel")


def press_ms(token: str) -> int:
    """How long a press token holds its keys"""
    if token.endswith("+"):
        return HOLD_MS["+"] * token.count("+")
    return HOLD_MS.get(token[-1], TAP_MS)


def duration_ms(script: str) -> int:
    """How long the firmware takes over SCRIPT"""
    return sum(int(t) if t.isdigit() else press_ms(t) + RELEASE_MS for t in tokens(script))
