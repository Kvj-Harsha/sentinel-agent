"""Back up and clear Sentinel's local tickets, incident memory, and audit events."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import sqlite3
import sys

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)
load_dotenv(REPO_ROOT / ".env")

from coded_tools.sentinel.incident_memory_store import DEFAULT_DATA_FILE as HISTORY_JSON  # noqa: E402
from coded_tools.sentinel.sqlite_store import DATA_DIR, SQLiteDatabase  # noqa: E402
from coded_tools.sentinel.ticket_store import DEFAULT_DATA_FILE as TICKETS_JSON  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm the local data reset")
    parser.add_argument("--database-only", action="store_true", help="reset only SQLite; preserve read-only legacy JSON fixtures")
    options = parser.parse_args()
    if not options.yes:
        parser.error("This reset is destructive. Re-run with --yes after stopping Sentinel.")

    database = SQLiteDatabase()
    backup_root = Path(os.environ.get("SENTINEL_BACKUP_DIR", str(DATA_DIR / ".reset-backups")))
    backup_dir = backup_root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=False)
    if not options.database_only:
        for source in (TICKETS_JSON, HISTORY_JSON):
            if source.exists():
                shutil.copy2(source, backup_dir / source.name)
    if database.path.exists():
        backup_path = backup_dir / database.path.name
        source_uri = f"file:{database.path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            destination = sqlite3.connect(backup_path)
            try:
                source.backup(destination)
            finally:
                destination.close()

    database.initialize()
    with database.transaction() as connection:
        connection.execute("DELETE FROM tickets")
        connection.execute("DELETE FROM incident_history")
        connection.execute("DELETE FROM audit_events")
    if not options.database_only:
        for path in (TICKETS_JSON, HISTORY_JSON):
            path.write_text("[]\n", encoding="utf-8")

    print(f"Local Sentinel records cleared. Backup saved under: {backup_dir}")
    print("No ServiceNow, GitHub, Confluence, Prometheus, or Loki APIs were called.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
