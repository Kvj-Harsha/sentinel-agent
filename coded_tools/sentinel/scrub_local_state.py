"""Back up and scrub PII/credentials from Sentinel's local tickets and incident history."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
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
from coded_tools.sentinel.schemas import sanitize_record  # noqa: E402
from coded_tools.sentinel.sqlite_store import DATA_DIR, SQLiteDatabase  # noqa: E402
from coded_tools.sentinel.ticket_store import DEFAULT_DATA_FILE as TICKETS_JSON  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--yes", action="store_true", help="confirm local records may be rewritten")
    parser.add_argument("--database-only", action="store_true", help="scrub only SQLite; preserve read-only legacy JSON fixtures")
    options = parser.parse_args()
    if not options.yes:
        parser.error("This command rewrites local records. Re-run with --yes after stopping Sentinel.")

    database = SQLiteDatabase()
    backup_root = Path(os.environ.get("SENTINEL_BACKUP_DIR", str(DATA_DIR / ".reset-backups")))
    backup_dir = backup_root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-scrub")
    backup_dir.mkdir(parents=True, exist_ok=False)
    if not options.database_only:
        for source in (TICKETS_JSON, HISTORY_JSON):
            if source.exists():
                shutil.copy2(source, backup_dir / source.name)
    if database.path.exists():
        source_uri = f"file:{database.path.resolve().as_posix()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True) as source:
            destination = sqlite3.connect(backup_dir / database.path.name)
            try:
                source.backup(destination)
            finally:
                destination.close()

    # Initialization applies the one-time scrub migration to SQLite records.
    database.initialize()

    counts = {}
    for path in (() if options.database_only else (TICKETS_JSON, HISTORY_JSON)):
        if not path.exists():
            continue
        rows = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(rows, list):
            raise ValueError(f"Expected a JSON list in {path.name}.")
        safe_rows = sanitize_record(rows)
        path.write_text(json.dumps(safe_rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        counts[path.name] = len(safe_rows)

    print(f"Local records scrubbed: {counts}. Backup saved under: {backup_dir}")
    print("No external APIs were called. Backups contain pre-scrub data; delete them when no longer needed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
