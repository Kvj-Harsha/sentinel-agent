"""Coded tool that opens a new synthetic ServiceNow-style incident ticket."""

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name

from coded_tools.sentinel import servicenow_client
from coded_tools.sentinel.ticket_store import TicketStore

logger = logging.getLogger(__name__)

DEFAULT_CALLER = "monitoring-integration"
DEFAULT_ASSIGNEE = "on-call-sre"
DEFAULT_GROUP = "Site Reliability"
TICKET_NUMBER_KEY = "sentinel_ticket_number"


class OpenTicket(CodedTool):
    """
    Opens a new incident ticket shaped like a real ServiceNow Incident table row (number,
    short_description, state, priority, assigned_to, work_notes, caller_id, timestamps).
    Always recorded in the local JSON store (backend/coded_tools/sentinel/data/tickets.json). When
    SERVICENOW_INSTANCE / SERVICENOW_USER / SERVICENOW_PASSWORD are set, the incident is ALSO created on
    that real ServiceNow instance through the Table API, and the real INC number replaces the synthetic one.
    If the instance is unreachable the ticket stays local and the failure is noted on it.

    Idempotent per conversation via sly_data (the same pattern record_incident uses for its
    session id): a retried or duplicated call within the same run returns the ticket already
    opened instead of creating a second one. This matters because an LLM-level "only call this
    once" instruction is not reliable protection against the framework itself retrying a step
    -- a real duplicate-ticket bug caught during manual testing is what prompted this.
    """

    def __init__(self, store: Optional[TicketStore] = None) -> None:
        """
        :param store: the ticket store to use. Defaults to the shared production store;
            tests inject one backed by a temporary file instead.
        """
        self.store = store or TicketStore()

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - service_name: the affected service.
            - short_description: a one-line summary of the incident for the ticket.
            - priority: optional priority ("1 - Critical", "2 - High", "3 - Moderate").
                Defaults to "2 - High".

        :param sly_data: reads/writes "sentinel_ticket_number": if a ticket was already opened
            in this conversation, returns it instead of opening a duplicate.

        :return: a dict with the ticket's number and fields (newly opened, or the existing one
            for this conversation), or an error string.
        """
        existing_number = sly_data.get(TICKET_NUMBER_KEY)
        if existing_number:
            existing = self.store.get(existing_number)
            if existing is not None:
                logger.debug("OpenTicket reusing existing ticket %s for this conversation", existing_number)
                return {
                    "number": existing["number"],
                    "state": existing["state"],
                    "priority": existing["priority"],
                    "assigned_to": existing["assigned_to"],
                    "summary": f"Ticket {existing['number']} was already opened for this incident; not duplicating.",
                }

        service_name: str = canonical_service_name(args.get("service_name") or "")
        short_description: str = (args.get("short_description") or "").strip()
        priority: str = (args.get("priority") or "2 - High").strip()

        missing = [
            name
            for name, val in (("service_name", service_name), ("short_description", short_description))
            if not val
        ]
        if missing:
            return f"Error: Missing required argument(s): {', '.join(missing)}."

        now = datetime.now(timezone.utc).isoformat()
        ticket = {
            "number": self.store.next_number(),
            "service_name": service_name,
            "short_description": short_description,
            "state": "In Progress",
            "priority": priority,
            "assigned_to": DEFAULT_ASSIGNEE,
            "assignment_group": DEFAULT_GROUP,
            "caller_id": DEFAULT_CALLER,
            "work_notes": [f"{now}: Ticket opened automatically from an incoming alert for '{service_name}'."],
            "sys_created_on": now,
            "sys_updated_on": now,
        }
        self._mirror_to_servicenow(ticket, priority)
        self.store.append(ticket)
        sly_data[TICKET_NUMBER_KEY] = ticket["number"]
        logger.debug("OpenTicket created %s for service=%r", ticket["number"], service_name)

        return {
            "number": ticket["number"],
            "state": ticket["state"],
            "priority": ticket["priority"],
            "assigned_to": ticket["assigned_to"],
            "summary": (
                f"Opened ticket {ticket['number']} ({priority}) for '{service_name}', "
                f"assigned to {DEFAULT_ASSIGNEE}."
            ),
        }

    @staticmethod
    def _mirror_to_servicenow(ticket: Dict[str, Any], priority: str) -> None:
        """Creates the incident on a real instance if configured; records the outcome on the ticket."""
        if not servicenow_client.is_configured():
            return
        level = priority.strip()[:1] if priority.strip()[:1] in ("1", "2", "3") else "2"
        try:
            record = servicenow_client.create_incident(
                short_description=ticket["short_description"],
                description=f"Opened by Sentinel for service '{ticket['service_name']}'.",
                priority_level=level,
            )
        except servicenow_client.ServiceNowError as exc:
            logger.warning("ServiceNow create failed, keeping ticket local: %s", exc)
            ticket["work_notes"].append(f"ServiceNow sync failed (ticket kept locally): {exc}")
            return
        ticket["number"] = record["number"]
        ticket["servicenow_sys_id"] = record["sys_id"]
        ticket["caller_id"] = record["caller"]
        ticket["assigned_to"] = record["assignee"]
        ticket["assignment_group"] = record["group"]
        ticket["servicenow_url"] = servicenow_client.record_url(record["sys_id"])
        ticket["work_notes"].append(f"Created on ServiceNow as {record['number']}.")

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
