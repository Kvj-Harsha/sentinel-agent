"""Makes backend/ importable so tests can use `coded_tools.sentinel.*` and `alerts` directly."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
