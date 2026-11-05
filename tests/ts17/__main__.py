"""Runs every test_* function in this package's test_* modules."""

from __future__ import annotations

import importlib
import pkgutil
import sys
import traceback
from pathlib import Path

from tests.ts17 import Skip


def main() -> int:
    failed = skipped = passed = 0
    for m in pkgutil.iter_modules([str(Path(__file__).parent)]):
        if not m.name.startswith("test_"):
            continue
        mod = importlib.import_module(f"tests.ts17.{m.name}")
        for name, fn in vars(mod).items():
            if not (name.startswith("test_") and callable(fn)):
                continue
            try:
                fn()
            except Skip as e:
                skipped += 1
                print(f"skip {m.name}.{name}: {e}")
                continue
            except Exception:
                failed += 1
                print(f"FAIL {m.name}.{name}")
                traceback.print_exc()
                continue
            passed += 1
            print(f"ok   {m.name}.{name}")
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
