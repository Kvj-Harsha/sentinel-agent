"""Container health check for a Sentinel UI or token-protected webhook service."""

import argparse
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def is_healthy(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as response:
            return response.status == 200
    except (OSError, URLError, TimeoutError):
        return False


parser = argparse.ArgumentParser()
parser.add_argument("target", choices=("ui", "webhook"))
target = parser.parse_args().target
url = "http://127.0.0.1:8501/healthz" if target == "ui" else "http://127.0.0.1:8090/healthz"
if not is_healthy(url):
    raise SystemExit(1)
if target == "ui":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from coded_tools.sentinel.sqlite_store import SQLiteDatabase  # noqa: E402

    SQLiteDatabase().initialize()
raise SystemExit(0)
