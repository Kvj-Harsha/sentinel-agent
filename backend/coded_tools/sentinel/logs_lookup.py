"""Coded tool that looks up synthetic service logs/metrics around an incident."""

import json
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name
from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import redact_text
from coded_tools.sentinel.schemas import sanitize_record
from coded_tools.sentinel.schemas import unavailable_result
from coded_tools.sentinel.schemas import utc_now

from coded_tools.sentinel.ticket_notes import add_note
from coded_tools.sentinel.verified_facts import publish_evidence

logger = logging.getLogger(__name__)

DATA_FILE = Path(__file__).parent / "data" / "logs.json"

# Words too generic to count as free-text query evidence on their own. Without this (and
# without token-based matching at all), a realistic multi-word query like "payment
# authorization failures" never matched anything, because the original implementation
# required the ENTIRE query string to appear verbatim as a substring -- caught by the
# automated eval harness (eval/run_eval.py), see summary.md.
QUERY_STOPWORDS = frozenset(
    {
        "a", "an", "the", "on", "after", "for", "from", "with", "to", "of", "in", "at",
        "and", "or", "is", "are", "this", "that", "failing", "failure", "failures",
    }
)


class LogsLookup(CodedTool):
    """
    Looks up error/anomaly log entries for a given service, optionally filtered by keywords.
    Backed entirely by a small synthetic dataset (backend/coded_tools/sentinel/data/logs.json) --
    no real production logs or PII are involved.
    """

    ANOMALY_LEVELS = {"ERROR", "WARN"}

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - service_name: the name of the service to inspect (e.g. "checkout-service").
            - query: optional free-text keywords to match against log messages.
            - include_baseline: optional bool (default False). When False (the default),
              only anomalous entries (level ERROR/WARN or status_code >= 400) are returned,
              so normal/nominal traffic entries don't drown out the actual incident signal.

        :param sly_data: unused.

        :return: a dict with the matching log entries sorted by timestamp (oldest first), or an error string.
        """
        service_name: str = canonical_service_name(args.get("service_name") or "")
        query: str = (args.get("query") or "").strip().lower()
        include_baseline: bool = bool(args.get("include_baseline", False))

        if not service_name and not query:
            return unavailable_result("fixture:logs.json", "Please provide a service_name or query keyword to search logs.")

        try:
            entries: List[Dict[str, Any]] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            return unavailable_result("fixture:logs.json", f"Could not read logs dataset: {exc}")

        matches = []
        for entry in entries:
            service_ok = (not service_name) or service_name in entry["service"].lower()
            query_ok = (not query) or self._query_matches(query, entry)
            is_anomaly = entry.get("level") in self.ANOMALY_LEVELS or entry.get("status_code", 0) >= 400
            anomaly_ok = include_baseline or is_anomaly
            if service_ok and query_ok and anomaly_ok:
                matches.append(entry)

        matches.sort(key=lambda e: e["timestamp"])
        logger.debug("LogsLookup matched %d entries for service=%r query=%r", len(matches), service_name, query)

        if not matches:
            return {
                "matches": [],
                "evidence": [],
                "source": "fixture:logs.json",
                "source_status": SourceStatus.FIXTURE.value,
                "summary": (
                    "No matching anomalous log entries found for the given service/query. "
                    "Pass include_baseline=true to also see normal traffic."
                ),
            }

        first, last = matches[0], matches[-1]
        errors = sum(m.get("count", 1) for m in matches if m.get("level") == "ERROR")
        note = (
            f"[Logs] {len(matches)} anomalous entries for {first['service']} between {first['timestamp']} and "
            f"{last['timestamp']} ({errors} errors). First: {first['level']} {first.get('status_code', '')} - "
            f"{redact_text(first['message'])}"
        )
        add_note(sly_data, note, once_key=f"logs:{first['service']}")
        retrieved_at = utc_now()
        evidence = [dump_model(EvidenceRecord(
            evidence_type="log", service_name=entry["service"], source="fixture:logs.json",
            source_status=SourceStatus.FIXTURE, query=query or service_name,
            record_id=entry.get("trace_id"), event_time=entry.get("timestamp"), retrieved_at=retrieved_at,
            summary=f"{entry.get('level', 'LOG')} {entry.get('status_code', '')}: {entry.get('message', '')}",
            safe_fields={key: entry[key] for key in ("service", "timestamp", "level", "status_code", "count", "trace_id", "pod", "message") if key in entry},
        )) for entry in matches]
        publish_evidence(sly_data, evidence)
        safe_matches = sanitize_record([{key: value for key, value in entry.items() if key != "stack_trace"} for entry in matches])
        return {"matches": safe_matches, "evidence": evidence, "source": "fixture:logs.json",
                "source_status": SourceStatus.FIXTURE.value,
                "summary": f"Found {len(matches)} matching anomalous log entries."}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)

    @staticmethod
    def _query_matches(query: str, entry: Dict[str, Any]) -> bool:
        """
        Token-based free-text match: true if any non-stopword query token is a substring of
        the entry's combined message/service/level text. More forgiving than requiring the
        whole query phrase to appear verbatim, which real incident-report phrasing rarely does.
        """
        combined = " ".join(str(entry.get(field, "")) for field in ("message", "service", "level")).lower()
        tokens = set(query.replace("-", " ").split()) - QUERY_STOPWORDS
        return any(token in combined for token in tokens)
