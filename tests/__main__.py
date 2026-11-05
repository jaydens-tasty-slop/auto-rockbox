"""Runs every offline suite: python -m tests [suite ...]  (default: all of SUITES).

A suite is a package under tests/; each test_* module's test functions (named
test or test_*) run in turn.  A test raises tests.Skip when its inputs are absent.
tests/host (unittest based, owned elsewhere) is not run here yet.
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

from tests import Skip

SUITES = ("ts17", "yp3", "fm")


def main(suites: list[str] | None = None) -> int:
    failed = skipped = passed = 0
    for suite in suites or SUITES:
        for m in pkgutil.iter_modules([str(Path(__file__).parent / suite)]):
            if not m.name.startswith("test_"):
                continue
            mod = importlib.import_module(f"tests.{suite}.{m.name}")
            label = f"{suite}.{m.name}"
            for name, fn in vars(mod).items():
                if not (name == "test" or name.startswith("test_")) or not callable(fn):
                    continue
                try:
                    fn()
                except Skip as e:
                    skipped += 1
                    print(f"skip {label}.{name}: {e}")
                except Exception:
                    failed += 1
                    print(f"FAIL {label}.{name}")
                    traceback.print_exc()
                else:
                    passed += 1
                    print(f"ok   {label}.{name}")
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or None))
