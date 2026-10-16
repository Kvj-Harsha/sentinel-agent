"""Standalone process entry point for the private Alertmanager webhook service."""

from __future__ import annotations

import signal
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "apps" / "sentinel"))

from alertmanager_receiver import receiver_status  # noqa: E402
from alertmanager_receiver import start_receiver  # noqa: E402
from alertmanager_receiver import stop_receiver  # noqa: E402


def main() -> int:
    """Start the listener and stop it cleanly when Docker or the operator sends SIGTERM."""
    stopped = threading.Event()
    start_receiver()
    if receiver_status()["status"] != "listening":
        return 1

    def request_shutdown(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    try:
        stopped.wait()
    finally:
        stop_receiver()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
