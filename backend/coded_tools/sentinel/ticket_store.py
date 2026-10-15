"""JSON-file store for synthetic ServiceNow-style incident tickets."""

import json
import logging
import threading
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

logger = logging.getLogger(__name__)

# Specialist agents run in parallel threads in one process and each read-modify-writes the ticket file; without
# a lock one agent's note could overwrite another's.
_LOCK = threading.RLock()

DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "tickets.json"
NUMBER_PREFIX = "INC"
NUMBER_START = 1200001


class TicketStore:
    """
    Reads and writes the synthetic ServiceNow-shaped ticket log
    (backend/coded_tools/sentinel/data/tickets.json by default). Each record mirrors the fields a
    real ServiceNow Incident table row would carry (number, short_description, state,
    priority, assigned_to, work_notes, caller_id, sys_created_on, sys_updated_on), so
    Sentinel's output slots into a familiar enterprise ticketing shape instead of a
    bespoke JSON blob.
    """

    def __init__(self, data_file: Optional[Path] = None) -> None:
        """
        :param data_file: path to the JSON store. Defaults to the shared production file;
            tests pass a temporary path so they don't mutate real/seeded data.
        """
        self.data_file = data_file or DEFAULT_DATA_FILE

    def load(self) -> List[Dict[str, Any]]:
        """
        :return: every ticket on record, oldest first. An empty list if the file is
            missing or empty (e.g. on first run).
        """
        if not self.data_file.exists():
            return []
        try:
            content = self.data_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read ticket store file %s: %s", self.data_file, exc)
            return []
        return json.loads(content) if content.strip() else []

    def _save(self, tickets: List[Dict[str, Any]]) -> None:
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(json.dumps(tickets, indent=2), encoding="utf-8")

    def next_number(self) -> str:
        """:return: the next sequential ServiceNow-style ticket number, e.g. 'INC1200003'."""
        return f"{NUMBER_PREFIX}{NUMBER_START + len(self.load())}"

    def append(self, ticket: Dict[str, Any]) -> Dict[str, Any]:
        """
        Appends one new ticket record.

        :param ticket: the ticket record to store.
        :return: the same ticket record, for convenience.
        """
        with _LOCK:
            tickets = self.load()
            tickets.append(ticket)
            self._save(tickets)
        return ticket

    def get(self, number: str) -> Optional[Dict[str, Any]]:
        """
        :param number: the ticket number to look up, e.g. 'INC1200001'.
        :return: the matching ticket record, or None if not found.
        """
        for ticket in self.load():
            if ticket.get("number") == number:
                return ticket
        return None

    def update(self, number: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Merges changes into the ticket matching the given number and persists the result.

        :param number: the ticket number to update, e.g. 'INC1200001'.
        :param changes: fields to merge in (e.g. state, sys_updated_on). The special key "append_work_notes"
            (a list of strings) is appended to the existing work notes atomically instead of replacing them.
        :return: the updated ticket record, or None if no ticket with that number exists.
        """
        with _LOCK:
            tickets = self.load()
            for ticket in tickets:
                if ticket.get("number") == number:
                    changes = dict(changes)
                    appended = changes.pop("append_work_notes", [])
                    ticket.update(changes)
                    if appended:
                        ticket["work_notes"] = list(ticket.get("work_notes", [])) + appended
                    self._save(tickets)
                    return ticket
        return None
