"""PyInstaller runtime hook: 把 cwd 切到 exe 所在目录。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    os.chdir(Path(sys.executable).resolve().parent)
