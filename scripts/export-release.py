#!/usr/bin/env -S /usr/bin/python3 -E -s -B
# SPDX-License-Identifier: 0BSD
"""Create an explicitly confirmed local source export; never publish it."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from release_export import main

if __name__ == '__main__':
    raise SystemExit(main())
