"""Import every submodule of a package, so a new file registers itself."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable


def import_all(package: str, path: Iterable[str]) -> None:
    for m in sorted(pkgutil.iter_modules(path), key=lambda m: m.name):
        if not m.name.startswith("_"):
            importlib.import_module(f"{package}.{m.name}")
