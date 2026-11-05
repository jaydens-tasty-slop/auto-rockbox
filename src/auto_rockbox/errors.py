"""Failures `arb` reports as one line (`arb: ...`, exit status 1) rather
than a traceback: a device that is missing, busy or refusing, bad input, a
feature the player lacks. Every module's own error class derives from
ArbError. ARB_TRACEBACK=1 in the environment shows the traceback anyway."""

from __future__ import annotations


class ArbError(Exception):
    pass


class UsageError(ArbError):
    """A bad argument that argparse could not catch"""
