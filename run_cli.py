#!/usr/bin/env python
# SPDX-License-Identifier: MIT
"""命令行入口：python run_cli.py <URL> [选项]"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ghdl.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
