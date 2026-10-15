"""Append-only JSON-file store for resolved Sentinel incidents."""

import json
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional

logger = logging.getLogger(__name__)

DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "incident_history.json"


class IncidentMemoryStore:
    """
    Reads and appends to the synthetic, append-only incident-history log
    (backend/coded_tools/sentinel/data/incident_history.json by default). This is what gives the
    Sentinel network a persistent-memory "learning loop" across separate conversations,
    rather than only ever reasoning over the static logs/deploys/runbooks fixtures.
    """

    def __init__(self, data_file: Optional[Path] = None) -> None:
        """
        :param data_file: path to the JSON store. Defaults to the shared production file;
            tests pass a temporary path so they don't mutate real/seeded data.
        """
        self.data_file = data_file or DEFAULT_DATA_FILE

    def load(self) -> List[Dict[str, Any]]:
        """
        :return: every recorded incident, oldest first. An empty list if the file is
            missing or empty (e.g. on first run).
        """
        if not self.data_file.exists():
            return []
        try:
            content = self.data_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read incident history file %s: %s", self.data_file, exc)
            return []
        return json.loads(content) if content.strip() else []

    def append(self, record: Dict[str, Any]) -> int:
        """
        Appends one incident record to the history file.

        :param record: the incident record to store (service_name, hypothesis, verdict,
            runbook_id, summary, session_id, recorded_at).
        :return: the total number of incidents now recorded for this record's service.
        """
        history = self.load()
        history.append(record)
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.data_file.write_text(json.dumps(history, indent=2), encoding="utf-8")
        service_name = record.get("service_name", "")
        return sum(1 for entry in history if entry.get("service_name") == service_name)

    def recent_for_service(self, service_name: str, limit: int = 5) -> List[Dict[str, Any]]:
        """
        :param service_name: the service to look up prior incidents for.
        :param limit: maximum number of records to return, most recent first.
        :return: prior recorded incidents for this service, most recent first.
        """
        matches = [entry for entry in self.load() if entry.get("service_name") == service_name]
        return list(reversed(matches))[:limit]
