"""Swappable JSON/SQLite store for Sentinel's resolved-incident history."""

from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from coded_tools.sentinel.sqlite_store import SQLiteDatabase
from coded_tools.sentinel.schemas import sanitize_record

DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "incident_history.json"
_LOCK = threading.RLock()


class IncidentMemoryStore:
    """Append-only incident history with transactional SQLite as the default backend."""

    def __init__(
        self,
        data_file: Optional[Path] = None,
        *,
        db_path: Optional[Path] = None,
        backend: Optional[str] = None,
    ) -> None:
        selected = (backend or ("json" if data_file is not None else os.getenv("SENTINEL_STORAGE_BACKEND", "sqlite"))).lower()
        if selected not in {"json", "sqlite"}:
            raise ValueError("SENTINEL_STORAGE_BACKEND must be 'sqlite' or 'json'.")
        self.backend = selected
        self.data_file = data_file or DEFAULT_DATA_FILE if selected == "json" else None
        self.db = SQLiteDatabase(db_path) if selected == "sqlite" else None

    def load(self) -> List[Dict[str, Any]]:
        if self.backend == "json":
            if not self.data_file.exists():
                return []
            try:
                content = self.data_file.read_text(encoding="utf-8")
            except OSError:
                return []
            return sanitize_record(json.loads(content)) if content.strip() else []
        with self.db.connection() as connection:
            rows = connection.execute("SELECT data_json FROM incident_history ORDER BY id").fetchall()
        return [sanitize_record(json.loads(row["data_json"])) for row in rows]

    def append(self, record: Dict[str, Any]) -> int:
        return self.append_if_new(record)[1]

    def append_if_new(self, record: Dict[str, Any]) -> tuple[bool, int]:
        record = sanitize_record(record)
        if self.backend == "json":
            with _LOCK:
                history = self.load()
                session_id = record.get("session_id")
                if session_id and any(item.get("session_id") == session_id for item in history):
                    return False, sum(1 for item in history if item.get("service_name") == record.get("service_name"))
                history.append(record)
                self.data_file.parent.mkdir(parents=True, exist_ok=True)
                self.data_file.write_text(json.dumps(history, indent=2), encoding="utf-8")
            return True, sum(1 for item in history if item.get("service_name") == record.get("service_name"))

        with self.db.transaction() as connection:
            session_id = record.get("session_id")
            if session_id and connection.execute("SELECT 1 FROM incident_history WHERE session_id = ?", (session_id,)).fetchone():
                row = connection.execute(
                    "SELECT COUNT(*) AS total FROM incident_history WHERE service_name = ?",
                    (record.get("service_name", ""),),
                ).fetchone()
                return False, int(row["total"])
            connection.execute(
                """INSERT INTO incident_history (session_id, fingerprint, service_name, recorded_at, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    record.get("session_id"), record.get("alert_fingerprint"), record.get("service_name", ""),
                    record.get("recorded_at"), json.dumps(record, ensure_ascii=False),
                ),
            )
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM incident_history WHERE service_name = ?",
                (record.get("service_name", ""),),
            ).fetchone()
            self.db.append_audit_event(
                connection, event_type="incident.recorded", entity_type="incident",
                entity_id=str(record.get("session_id") or record.get("recorded_at") or "unknown"),
                fingerprint=record.get("alert_fingerprint"), session_id=record.get("session_id"),
                details={"service_name": record.get("service_name"), "runbook_id": record.get("runbook_id")},
            )
            return True, int(row["total"])

    def recent_for_service(self, service_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self.backend == "json":
            matches = [entry for entry in self.load() if entry.get("service_name") == service_name]
            return list(reversed(matches))[:limit]
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT data_json FROM incident_history WHERE service_name = ? ORDER BY id DESC LIMIT ?",
                (service_name, max(0, limit)),
            ).fetchall()
        return [sanitize_record(json.loads(row["data_json"])) for row in rows]

    def find_by_session_id(self, session_id: str) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        if self.backend == "json":
            return next((entry for entry in self.load() if entry.get("session_id") == session_id), None)
        with self.db.connection() as connection:
            row = connection.execute("SELECT data_json FROM incident_history WHERE session_id = ?", (session_id,)).fetchone()
        return sanitize_record(json.loads(row["data_json"])) if row else None
