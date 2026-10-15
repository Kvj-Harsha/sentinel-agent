"""Coded tool that records a resolved incident into the persistent incident-history store."""

import logging
import uuid
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name

from coded_tools.sentinel.incident_memory_store import IncidentMemoryStore

logger = logging.getLogger(__name__)

SESSION_ID_KEY = "incident_session_id"


class RecordIncident(CodedTool):
    """
    Records one resolved incident (service, hypothesis, verdict, runbook match) into the
    persistent incident-history store, so future triages of the same service can recall it.

    Threads a session id through `sly_data` rather than the LLM-visible chat stream: the id
    only correlates records from the same conversation internally and has no bearing on the
    triage itself, so there's no reason to spend chat tokens on it or let the model see or
    invent it.
    """

    def __init__(self, store: Optional[IncidentMemoryStore] = None) -> None:
        """
        :param store: the memory store to use. Defaults to the shared production store;
            tests inject one backed by a temporary file instead.
        """
        self.store = store or IncidentMemoryStore()

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - service_name: the affected service.
            - hypothesis: the root-cause hypothesis that was evaluated.
            - verdict: the correlation_check verdict ("SUPPORTED" or "NOT_SUPPORTED").
            - summary: a one-sentence summary of the incident and outcome.
            - runbook_id: optional id of the matching runbook, if any was found.

        :param sly_data: a dictionary whose values are kept out of the chat stream.
            Reads/writes "incident_session_id": a session id correlating records from the
            same conversation. Generated and stored here if not already present.

        :return: a dict confirming the record was stored, or an error string.
        """
        service_name: str = canonical_service_name(args.get("service_name") or "")
        hypothesis: str = (args.get("hypothesis") or "").strip()
        verdict: str = (args.get("verdict") or "").strip()
        summary: str = (args.get("summary") or "").strip()
        runbook_id = args.get("runbook_id") or None

        missing = [
            name
            for name, val in (
                ("service_name", service_name),
                ("hypothesis", hypothesis),
                ("verdict", verdict),
                ("summary", summary),
            )
            if not val
        ]
        if missing:
            return f"Error: Missing required argument(s): {', '.join(missing)}."

        session_id = sly_data.get(SESSION_ID_KEY)
        if not session_id:
            session_id = str(uuid.uuid4())
            sly_data[SESSION_ID_KEY] = session_id

        # Idempotent per incident session: a retried or repeated call must not store a duplicate
        # (a live run once recorded the same incident twice).
        if any(entry.get("session_id") == session_id for entry in self.store.load()):
            total = len(self.store.recent_for_service(service_name, limit=1_000_000))
            return {
                "stored": False,
                "total_incidents_for_service": total,
                "summary": f"Already recorded for this incident ({total} on record for '{service_name}'); not duplicating.",
            }

        record = {
            "session_id": session_id,
            "service_name": service_name,
            "hypothesis": hypothesis,
            "verdict": verdict,
            "runbook_id": runbook_id,
            "summary": summary,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        total_for_service = self.store.append(record)
        logger.debug("RecordIncident stored record for %r (total now %d)", service_name, total_for_service)

        return {
            "stored": True,
            "total_incidents_for_service": total_for_service,
            "summary": f"Recorded. This is incident #{total_for_service} on record for '{service_name}'.",
        }

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
