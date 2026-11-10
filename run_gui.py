#!/usr/bin/env python
# SPDX-License-Identifier: MIT
"""启动图形界面（双击 .bat 或运行本文件）。"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ghdl.gui import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
