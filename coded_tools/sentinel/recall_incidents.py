"""Coded tool that recalls prior resolved incidents for a service from persistent memory."""

import logging
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name

from coded_tools.sentinel.incident_memory_store import IncidentMemoryStore
from coded_tools.sentinel.verified_facts import publish

logger = logging.getLogger(__name__)


class RecallIncidents(CodedTool):
    """
    Looks up prior resolved incidents for a service from the persistent incident-history
    store, so the network can notice recurring patterns ("this is the 3rd similar incident
    this month") instead of treating every incident as a one-off.
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
            - service_name: the service to look up prior incidents for.

        :param sly_data: prior-incident facts are published to sly_data["sentinel_verified"]["memory"].

        :return: a dict with prior incidents (most recent first) and a recurrence flag.
        """
        service_name: str = canonical_service_name(args.get("service_name") or "")
        if not service_name:
            return "Error: Please provide a service_name to recall prior incidents for."

        prior = self.store.recent_for_service(service_name)
        logger.debug("RecallIncidents found %d prior incident(s) for %r", len(prior), service_name)

        if not prior:
            publish(sly_data, "memory", {"prior_incident_count": 0, "suggested_hotfix": None})
            return {
                "prior_incidents": [],
                "is_recurring": False,
                "suggested_hotfix": None,
                "summary": f"No prior recorded incidents found for '{service_name}'.",
            }

        runbook_ids = {entry.get("runbook_id") for entry in prior if entry.get("runbook_id")}
        is_recurring = len(prior) >= 2
        recurrence_note = ""
        if is_recurring and len(runbook_ids) == 1:
            recurrence_note = f" All point to the same runbook ({next(iter(runbook_ids))})."

        suggested_hotfix = self._suggest_hotfix(prior) if is_recurring else None
        publish(sly_data, "memory", {"prior_incident_count": len(prior), "suggested_hotfix": suggested_hotfix})

        return {
            "prior_incidents": prior,
            "is_recurring": is_recurring,
            "suggested_hotfix": suggested_hotfix,
            "summary": (
                f"Found {len(prior)} prior recorded incident(s) for '{service_name}'."
                f"{recurrence_note}"
            ),
        }

    @staticmethod
    def _suggest_hotfix(prior: List[Dict[str, Any]]) -> Optional[str]:
        """
        Derives an actionable suggestion from the most recent prior incident, rather than
        leaving recurrence as a bare observation -- the known fix that worked last time is
        more useful to an on-call engineer than "this happened before."

        :param prior: prior incidents for the service, most recent first.
        :return: a one-sentence actionable suggestion, or None if nothing usable is on record.
        """
        last = prior[0]
        runbook_id = last.get("runbook_id")
        last_summary = (last.get("summary") or "").strip()
        if not runbook_id:
            return last_summary or None
        if last_summary:
            return f"Reapply the fix from {runbook_id}: {last_summary}"
        return f"Reapply the fix from {runbook_id}."

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
