"""python -m tests.ts17: the TS17 suite alone (python -m tests runs them all)."""

import sys

from tests.__main__ import main

sys.exit(main(["ts17"]))
