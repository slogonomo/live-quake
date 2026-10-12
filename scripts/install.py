#!/usr/bin/env -S /usr/bin/python3 -E -s -B
# SPDX-License-Identifier: 0BSD
"""Explicit local-artifact installer. No service activation or desktop actions."""
import sys
sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))
from install import main

if __name__ == '__main__':
    raise SystemExit(main())
