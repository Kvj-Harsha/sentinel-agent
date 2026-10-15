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

from coded_tools.sentinel.ticket_notes import add_note

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
            return "Error: Please provide at least a service_name or a query keyword to search logs for."

        try:
            entries: List[Dict[str, Any]] = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except OSError as exc:
            return f"Error: Could not read logs dataset: {exc}"

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
                "summary": (
                    "No matching anomalous log entries found for the given service/query. "
                    "Pass include_baseline=true to also see normal traffic."
                ),
            }

        first, last = matches[0], matches[-1]
        errors = sum(m.get("count", 1) for m in matches if m.get("level") == "ERROR")
        stack = (first.get("stack_trace") or "").splitlines()
        note = (
            f"[Logs] {len(matches)} anomalous entries for {first['service']} between {first['timestamp']} and "
            f"{last['timestamp']} ({errors} errors). First: {first['level']} {first.get('status_code', '')} - "
            f"{first['message']}"
        )
        if stack:
            note += f" Trace {first.get('trace_id', '-')} on {first.get('pod', '-')}: {stack[0].strip()} | {stack[1].strip() if len(stack) > 1 else ''}"
        add_note(sly_data, note, once_key=f"logs:{first['service']}")
        return {"matches": matches, "summary": f"Found {len(matches)} matching anomalous log entries."}

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
