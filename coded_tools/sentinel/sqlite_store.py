"""Shared SQLite database for durable local Sentinel state."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any
from typing import Dict
from typing import Iterator

from coded_tools.sentinel.schemas import sanitize_record


DATA_DIR = Path(__file__).parent / "data"
DEFAULT_DB_PATH = Path(os.getenv("SENTINEL_DB_PATH", str(DATA_DIR / "sentinel.db")))
_INIT_LOCK = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE TABLE IF NOT EXISTS tickets (
    number TEXT PRIMARY KEY,
    fingerprint TEXT,
    session_id TEXT,
    servicenow_sys_id TEXT,
    repair_pr_number TEXT,
    created_at TEXT,
    updated_at TEXT,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tickets_fingerprint_idx ON tickets(fingerprint);
CREATE INDEX IF NOT EXISTS tickets_session_idx ON tickets(session_id);
CREATE TABLE IF NOT EXISTS incident_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    fingerprint TEXT,
    service_name TEXT NOT NULL DEFAULT '',
    recorded_at TEXT,
    data_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS incident_history_service_idx ON incident_history(service_name, id);
CREATE INDEX IF NOT EXISTS incident_history_fingerprint_idx ON incident_history(fingerprint);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    fingerprint TEXT,
    session_id TEXT,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    details_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS audit_events_entity_idx ON audit_events(entity_type, entity_id, id);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'active',
    ticket_number TEXT,
    updated_at TEXT,
    data_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alert_jobs (
    id TEXT PRIMARY KEY,
    updated_at TEXT,
    data_json TEXT NOT NULL
);
"""


def _text(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


class SQLiteDatabase:
    """Opens short-lived SQLite connections and imports the legacy JSON data once."""

    def __init__(self, path: Path | str | None = None, *, seed_legacy_data: bool | None = None) -> None:
        # Resolve the environment at construction time because the app entry point loads .env
        # after some modules may already have been imported.
        configured_path = Path(os.getenv("SENTINEL_DB_PATH", str(DATA_DIR / "sentinel.db")))
        self.path = Path(path) if path is not None else configured_path
        if seed_legacy_data is None:
            seed_from_legacy = self.path.resolve() == configured_path.resolve()
            self.seed_legacy_data = seed_from_legacy or os.getenv("SENTINEL_SEED_LEGACY_DATA", "false").strip().lower() in {"1", "true", "yes", "on"}
        else:
            self.seed_legacy_data = seed_legacy_data
        self._initialized = False
        self._instance_lock = threading.Lock()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._instance_lock, _INIT_LOCK:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect_raw()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.executescript(SCHEMA)
                connection.execute("BEGIN IMMEDIATE")
                if self.seed_legacy_data:
                    self._migrate_json_once(connection)
                self._sanitize_existing_records_once(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()
            self._initialized = True

    def _connect_raw(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=15000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _read_json(path: Path) -> list[Dict[str, Any]]:
        if not path.exists():
            return []
        content = path.read_text(encoding="utf-8")
        parsed = json.loads(content) if content.strip() else []
        if not isinstance(parsed, list):
            raise ValueError(f"Legacy state file must contain a JSON list: {path}")
        return [record for record in parsed if isinstance(record, dict)]

    def _migrate_json_once(self, connection: sqlite3.Connection) -> None:
        migration = "json-seed-v1"
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (migration,)).fetchone():
            return
        tickets_path = DATA_DIR / "tickets.json"
        history_path = DATA_DIR / "incident_history.json"
        for ticket in self._read_json(tickets_path):
            number = _text(ticket.get("number"))
            if not number:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO tickets
                   (number, fingerprint, session_id, servicenow_sys_id, repair_pr_number, created_at, updated_at, data_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    number,
                    _text(ticket.get("alert_fingerprint")),
                    _text(ticket.get("session_id")),
                    _text(ticket.get("servicenow_sys_id")),
                    _text(ticket.get("repair_pr_number")),
                    _text(ticket.get("sys_created_on")),
                    _text(ticket.get("sys_updated_on")),
                    json.dumps(sanitize_record(ticket), ensure_ascii=False),
                ),
            )
        for record in self._read_json(history_path):
            connection.execute(
                """INSERT INTO incident_history (session_id, fingerprint, service_name, recorded_at, data_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    _text(record.get("session_id")),
                    _text(record.get("alert_fingerprint")),
                    str(record.get("service_name") or ""),
                    _text(record.get("recorded_at")),
                    json.dumps(sanitize_record(record), ensure_ascii=False),
                ),
            )
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration,))

    @staticmethod
    def _sanitize_existing_records_once(connection: sqlite3.Connection) -> None:
        migration = "privacy-scrub-v1"
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (migration,)).fetchone():
            return
        for table in ("tickets", "incident_history"):
            rows = connection.execute(f"SELECT rowid AS record_rowid, data_json FROM {table}").fetchall()
            for row in rows:
                try:
                    safe_data = sanitize_record(json.loads(row["data_json"]))
                except (TypeError, ValueError):
                    safe_data = {}
                connection.execute(f"UPDATE {table} SET data_json = ? WHERE rowid = ?", (json.dumps(safe_data, ensure_ascii=False), row["record_rowid"]))
        rows = connection.execute("SELECT id, details_json FROM audit_events").fetchall()
        for row in rows:
            try:
                safe_details = sanitize_record(json.loads(row["details_json"]))
            except (TypeError, ValueError):
                safe_details = {}
            connection.execute("UPDATE audit_events SET details_json = ? WHERE id = ?", (json.dumps(safe_details, ensure_ascii=False), row["id"]))
        connection.execute("INSERT INTO schema_migrations(version) VALUES (?)", (migration,))

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect_raw()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        connection = self._connect_raw()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def save_conversation(self, conversation_id: str, data: Dict[str, Any], ticket_number: str | None = None) -> None:
        """Upsert the whole UI conversation (messages, chat context, sly_data) so a page reload can restore it."""
        payload = json.dumps(sanitize_record(data), ensure_ascii=False, default=str)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO conversations (id, status, ticket_number, updated_at, data_json) "
                "VALUES (?, 'active', ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), ?) "
                "ON CONFLICT(id) DO UPDATE SET ticket_number = COALESCE(excluded.ticket_number, ticket_number), "
                "updated_at = excluded.updated_at, data_json = excluded.data_json",
                (conversation_id, ticket_number, payload),
            )

    def archive_conversation(self, conversation_id: str) -> None:
        with self.transaction() as connection:
            connection.execute("UPDATE conversations SET status = 'archived' WHERE id = ?", (conversation_id,))

    def active_conversations(self, limit: int = 5) -> list[Dict[str, Any]]:
        """Newest active (not archived) conversations first, each with its id and ticket number."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id, ticket_number, data_json, updated_at FROM conversations WHERE status = 'active' "
                "ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"id": row[0], "ticket_number": row[1], **json.loads(row[2]), "updated_at": row[3]} for row in rows]

    def save_alert_job(self, job: Dict[str, Any]) -> None:
        """Upsert a webhook job so a UI running in another process can show it."""
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO alert_jobs (id, updated_at, data_json) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at, data_json = excluded.data_json",
                (job["id"], job.get("updated_at"), json.dumps(sanitize_record(job), ensure_ascii=False, default=str)),
            )

    def recent_alert_jobs(self, limit: int = 20) -> list[Dict[str, Any]]:
        """Newest webhook jobs first."""
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT data_json FROM alert_jobs ORDER BY updated_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def append_audit_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        fingerprint: str | None = None,
        session_id: str | None = None,
        details: Dict[str, Any] | None = None,
    ) -> None:
        # Explicit allowlist: audit rows must not capture ticket text, log bodies, or credentials.
        allowed = {"from_state", "to_state", "service_name", "priority", "connector", "outcome", "actor", "decision", "runbook_id", "repair_pr_number", "verdict", "evidence_ids"}
        safe_details = sanitize_record({key: value for key, value in (details or {}).items() if key in allowed and isinstance(value, (str, int, float, bool, type(None)))})
        connection.execute(
            """INSERT INTO audit_events (event_type, entity_type, entity_id, fingerprint, session_id, details_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event_type, entity_type, entity_id, fingerprint, session_id, json.dumps(safe_details, ensure_ascii=False)),
        )
