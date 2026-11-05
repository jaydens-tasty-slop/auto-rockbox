"""Offline tests: python -m tests runs every suite (see tests/__main__.py)."""


class Skip(Exception):
    """A test whose inputs (a toolchain, a build, a port checkout) this machine lacks"""
