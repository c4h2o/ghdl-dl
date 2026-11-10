# SPDX-License-Identifier: MIT
"""ghdl-dl —— GitHub 大文件多线程分段下载器。"""

from __future__ import annotations

__version__ = "1.0.0"

from .engine import DownloadJob, Result, Snapshot  # noqa: E402
from .model import human_eta, human_size  # noqa: E402

__all__ = ["DownloadJob", "Result", "Snapshot", "human_size", "human_eta", "__version__"]
