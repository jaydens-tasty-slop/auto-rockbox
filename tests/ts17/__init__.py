"""Offline tests of the TS17 target: python -m tests.ts17 (no player needed)."""


class Skip(Exception):
    """A test whose inputs (a toolchain, a build) this machine lacks"""
