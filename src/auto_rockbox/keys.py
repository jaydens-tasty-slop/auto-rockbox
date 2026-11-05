"""Key scripts: what a dev build presses by itself after boot.

    "9000 D D S 1500 UL P+++"

Tokens are separated by spaces or commas. A number waits that many ms. A run
of key letters presses those keys together (a chord), for a short tap; a
suffix changes the press:
    X+     held 1 s, one more second per further +  (X++ = 2 s)
    X*     held as a real long press (target-defined length)
    X~     a short tap through the real key path

Letters and suffixes are the target's (`Target.key_letters`,
`Target.key_suffixes`); the firmware parses the script, the host only checks
it so that a typo fails before a flash rather than after a boot.
"""

from __future__ import annotations

import re

TAP_MS = 350        # a tap and the gap after it, about


class KeyScriptError(ValueError):
    pass


def tokens(script: str) -> list[str]:
    return [t for t in re.split(r"[\s,]+", script.strip()) if t]


def check_script(script: str, letters: str, suffixes: str = "+") -> None:
    pat = re.compile(rf"\d+|[{re.escape(letters)}]+(\++|[{re.escape(suffixes)}])?")
    for t in tokens(script):
        if not pat.fullmatch(t):
            raise KeyScriptError(f"bad key-script token {t!r} (letters {letters}, "
                                 f"suffixes {suffixes})")


def duration_ms(script: str) -> int:
    """How long the script takes, about"""
    ms = 0
    for t in tokens(script):
        if t.isdigit():
            ms += int(t)
        else:
            ms += TAP_MS + 1000 * t.count("+")
    return ms
