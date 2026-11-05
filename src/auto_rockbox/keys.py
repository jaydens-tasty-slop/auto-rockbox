"""Key scripts: what a dev build presses by itself after boot (run config
keys=, shell `keys`; the firmware's dev_keys(), firmware/devlog.c).

    "9000 D D S 1500 UL P+++"

Tokens are separated by spaces or commas. A number waits that many ms. A run
of key letters presses those keys together (a chord) for a 150 ms tap; a
suffix changes the press:
    X+     held 1 s, one more second per further +  (X++ = 2 s)
    X*     held 1.2 s: a long press
    X~     held 1/8 s: a short tap
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


def check_script(script: str, letters: str, suffixes: str = SUFFIXES) -> None:
    pat = re.compile(rf"\d+|[{re.escape(letters)}]+(\++|[{re.escape(suffixes)}])?")
    for t in tokens(script):
        if not pat.fullmatch(t):
            raise KeyScriptError(f"bad key-script token {t!r} (letters {letters}, "
                                 f"suffixes {suffixes})")


def press_ms(token: str) -> int:
    """How long a press token holds its keys"""
    if token.endswith("+"):
        return HOLD_MS["+"] * token.count("+")
    return HOLD_MS.get(token[-1], TAP_MS)


def duration_ms(script: str) -> int:
    """How long the firmware takes over SCRIPT"""
    return sum(int(t) if t.isdigit() else press_ms(t) + RELEASE_MS for t in tokens(script))
