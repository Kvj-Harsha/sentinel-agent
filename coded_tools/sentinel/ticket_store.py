"""Swappable JSON/SQLite store for Sentinel incident tickets."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

from coded_tools.sentinel.sqlite_store import SQLiteDatabase
from coded_tools.sentinel.schemas import sanitize_record

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()

DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "tickets.json"
NUMBER_PREFIX = "INC"
NUMBER_START = 1200001


class TicketStore:
    """Ticket repository with transactional SQLite by default and an explicit JSON option."""

    def __init__(
        self,
        data_file: Optional[Path] = None,
        *,
        db_path: Optional[Path] = None,
        backend: Optional[str] = None,
    ) -> None:
        # An explicitly supplied legacy data_file keeps existing callers/tests on JSON.
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
            except OSError as exc:
                logger.warning("Could not read ticket store file %s: %s", self.data_file, exc)
                return []
            return sanitize_record(json.loads(content)) if content.strip() else []
        with self.db.connection() as connection:
            rows = connection.execute("SELECT data_json FROM tickets ORDER BY rowid").fetchall()
        return [sanitize_record(json.loads(row["data_json"])) for row in rows]

    def next_number(self) -> str:
        if self.backend == "json":
            existing = self.load()
        else:
            with self.db.connection() as connection:
                rows = connection.execute("SELECT number FROM tickets").fetchall()
            existing = [{"number": row["number"]} for row in rows]
        suffixes = [int(match.group(1)) for item in existing if (match := re.fullmatch(r"INC(\d+)", str(item.get("number", ""))))]
        return f"{NUMBER_PREFIX}{max(suffixes, default=NUMBER_START - 1) + 1}"

    def append(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        ticket = sanitize_record(ticket)
        if self.backend == "json":
            with _LOCK:
                tickets = self.load()
                fingerprint = ticket.get("alert_fingerprint")
                if fingerprint:
                    existing = next((item for item in reversed(tickets) if item.get("alert_fingerprint") == fingerprint), None)
                    if existing:
                        return existing
                if any(item.get("number") == ticket.get("number") for item in tickets):
                    ticket["number"] = self._next_number_from(tickets)
                tickets.append(ticket)
                self._save_json(tickets)
            return ticket

        with self.db.transaction() as connection:
            fingerprint = ticket.get("alert_fingerprint")
            if fingerprint:
                row = connection.execute(
                    "SELECT data_json FROM tickets WHERE fingerprint = ? ORDER BY rowid DESC LIMIT 1", (fingerprint,)
                ).fetchone()
                if row:
                    return json.loads(row["data_json"])
            if connection.execute("SELECT 1 FROM tickets WHERE number = ?", (ticket["number"],)).fetchone():
                row = connection.execute("SELECT number FROM tickets").fetchall()
                ticket["number"] = self._next_number_from([{"number": item["number"]} for item in row])
            connection.execute(
                """INSERT INTO tickets
                   (number, fingerprint, session_id, servicenow_sys_id, repair_pr_number, created_at, updated_at, data_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticket["number"], ticket.get("alert_fingerprint"), ticket.get("session_id"),
                    ticket.get("servicenow_sys_id"), ticket.get("repair_pr_number"),
                    ticket.get("sys_created_on"), ticket.get("sys_updated_on"),
                    json.dumps(ticket, ensure_ascii=False),
                ),
            )
            self.db.append_audit_event(
                connection, event_type="ticket.created", entity_type="ticket", entity_id=ticket["number"],
                fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                details={"service_name": ticket.get("service_name"), "priority": ticket.get("priority"), "to_state": ticket.get("state")},
            )
        return ticket

    @staticmethod
    def _next_number_from(tickets: List[Dict[str, Any]]) -> str:
        suffixes = [int(match.group(1)) for item in tickets if (match := re.fullmatch(r"INC(\d+)", str(item.get("number", ""))))]
        return f"{NUMBER_PREFIX}{max(suffixes, default=NUMBER_START - 1) + 1}"

    def _save_json(self, tickets: List[Dict[str, Any]]) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(json.dumps(tickets, indent=2), encoding="utf-8")

    def get(self, number: str) -> Optional[Dict[str, Any]]:
        if self.backend == "json":
            return next((ticket for ticket in self.load() if ticket.get("number") == number), None)
        with self.db.connection() as connection:
            row = connection.execute("SELECT data_json FROM tickets WHERE number = ?", (number,)).fetchone()
        return sanitize_record(json.loads(row["data_json"])) if row else None

    def find_by_fingerprint(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        if not fingerprint:
            return None
        if self.backend == "json":
            return next((item for item in reversed(self.load()) if item.get("alert_fingerprint") == fingerprint), None)
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT data_json FROM tickets WHERE fingerprint = ? ORDER BY rowid DESC LIMIT 1", (fingerprint,)
            ).fetchone()
        return sanitize_record(json.loads(row["data_json"])) if row else None

    def update(self, number: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        changes = sanitize_record(changes)
        if self.backend == "json":
            with _LOCK:
                tickets = self.load()
                for ticket in tickets:
                    if ticket.get("number") == number:
                        self._merge(ticket, changes)
                        self._save_json(tickets)
                        return ticket
            return None

        with self.db.transaction() as connection:
            row = connection.execute("SELECT data_json FROM tickets WHERE number = ?", (number,)).fetchone()
            if not row:
                return None
            ticket = json.loads(row["data_json"])
            previous_state = ticket.get("state")
            self._merge(ticket, changes)
            connection.execute(
                """UPDATE tickets SET fingerprint=?, session_id=?, servicenow_sys_id=?, repair_pr_number=?,
                   created_at=?, updated_at=?, data_json=? WHERE number=?""",
                (
                    ticket.get("alert_fingerprint"), ticket.get("session_id"), ticket.get("servicenow_sys_id"),
                    ticket.get("repair_pr_number"), ticket.get("sys_created_on"), ticket.get("sys_updated_on"),
                    json.dumps(ticket, ensure_ascii=False), number,
                ),
            )
            current_state = ticket.get("state")
            event_type = "ticket.state_transition" if current_state != previous_state else "ticket.updated"
            self.db.append_audit_event(
                connection, event_type=event_type, entity_type="ticket", entity_id=number,
                fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                details={"service_name": ticket.get("service_name"), "from_state": previous_state,
                         "to_state": current_state, "repair_pr_number": ticket.get("repair_pr_number")},
            )
            return ticket

    @staticmethod
    def _merge(ticket: Dict[str, Any], changes: Dict[str, Any]) -> None:
        changes = dict(changes)
        appended = changes.pop("append_work_notes", [])
        ticket.update(changes)
        if appended:
            ticket["work_notes"] = list(ticket.get("work_notes", [])) + appended

    def append_audit_event(
        self, event_type: str, entity_type: str, entity_id: str, *, fingerprint: str | None = None,
        session_id: str | None = None, details: Dict[str, Any] | None = None,
    ) -> None:
        if self.backend != "sqlite":
            return
        with self.db.transaction() as connection:
            self.db.append_audit_event(
                connection, event_type=event_type, entity_type=entity_type, entity_id=entity_id,
                fingerprint=fingerprint, session_id=session_id, details=details,
            )

    def audit_events(self, entity_type: str, entity_id: str) -> List[Dict[str, Any]]:
        if self.backend != "sqlite":
            return []
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT event_type, entity_type, entity_id, fingerprint, session_id, occurred_at, details_json "
                "FROM audit_events WHERE entity_type = ? AND entity_id = ? ORDER BY id", (entity_type, entity_id)
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details_json"])} for row in rows]
