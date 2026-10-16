"""Coded tool that updates/resolves a synthetic ServiceNow-style incident ticket."""

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel import servicenow_client
from coded_tools.sentinel.open_ticket import TICKET_NUMBER_KEY
from coded_tools.sentinel.remediation_pipeline import plan_exists
from coded_tools.sentinel.schemas import redact_text
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.verified_facts import VERIFIED_KEY

logger = logging.getLogger(__name__)


class UpdateTicket(CodedTool):
    """
    Updates an existing local ServiceNow-style ticket with a new work note and,
    optionally, a new state. Planned remediations remain In Progress until the HILT flow
    verifies a human-approved PR merge and app recovery. Updates the local SQLite store by default and, for a
    ticket that was created on a real ServiceNow instance, PATCHes that incident too (work note,
    and Resolved state with close code/notes).
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
            - number: the ticket number to update, e.g. 'INC1200001'. Optional if this
              conversation already opened one (via open_ticket) -- falls back to that.
            - work_note: a note to append describing what was found/done.
            - state: optional new state (e.g. "Resolved"). Omit to leave the state unchanged.

        :param sly_data: reads "sentinel_ticket_number", set by open_ticket earlier in this
            conversation, as a fallback if the caller omits (or mistypes) the ticket number.

        :return: a dict confirming the update, or an error string.
        """
        number: str = (args.get("number") or "").strip() or sly_data.get(TICKET_NUMBER_KEY, "")
        work_note: str = (args.get("work_note") or "").strip()
        state: str = (args.get("state") or "").strip()

        missing = [name for name, val in (("number", number), ("work_note", work_note)) if not val]
        if missing:
            return f"Error: Missing required argument(s): {', '.join(missing)}."

        existing = self.store.get(number)
        if existing is None:
            return f"Error: No ticket found with number '{number}'."

        # The ticket state follows tool-verified correlation and the HILT repair gate, not the model's say-so.
        verified = sly_data.get(VERIFIED_KEY) or {}
        verdict = (verified.get("correlation") or {}).get("verdict")
        # A passing local preflight is not approval to change code. Services with a remediation plan stay open
        # until the separate human-reviewed GitHub PR workflow verifies merge and recovery.
        remediation = verified.get("remediation") or {}
        fix_pending = plan_exists(existing.get("service_name", "")) and not (remediation.get("github_pr_merged") and remediation.get("recovery_verified"))
        if verdict == "SUPPORTED" and not fix_pending:
            state = "Resolved"
        elif state == "Resolved":
            state = ""

        work_note = redact_text(self._verified_note(verified, state))
        if any(note.endswith(work_note) for note in existing.get("work_notes", [])) and not state:
            return {
                "number": number,
                "state": existing["state"],
                "summary": f"Ticket {number} already carries this status; state is {existing['state']}.",
            }

        now = datetime.now(timezone.utc).isoformat()
        changes: Dict[str, Any] = {
            "append_work_notes": [f"{now}: {work_note}"],
            "sys_updated_on": now,
        }
        if state:
            changes["state"] = state

        remote_sync_ok = self._sync_to_servicenow(existing, changes, work_note, state)
        resolution_blocked = state == "Resolved" and existing.get("servicenow_sys_id") and servicenow_client.is_configured() and not remote_sync_ok
        if resolution_blocked:
            changes.pop("state", None)
            state = ""
            changes["append_work_notes"][0] = f"{now}: {self._verified_note(verified, '')}"
        updated = self.store.update(number, changes)
        logger.debug("UpdateTicket updated %s (state=%r)", number, updated.get("state") if updated else None)

        return {
            "number": number,
            "state": updated["state"],
            "summary": (
                f"Updated ticket {number}; state is {updated['state']}."
                if not resolution_blocked
                else f"Updated ticket {number}, but ServiceNow did not confirm resolution; state remains {updated['state']}."
            ),
            "resolution_blocked": bool(resolution_blocked),
        }

    @staticmethod
    def _verified_note(verified: Dict[str, Any], state: str) -> str:
        """
        Builds the work note from what the deterministic tools verified. The model's own free-text note is
        deliberately not written to the ticket: in testing it invented causes ("database connectivity") that no
        tool had found, and a ticket is a system of record.
        """
        parts = []
        correlation = verified.get("correlation")
        if not correlation:
            return "[Status] Investigation in progress."
        if correlation:
            parts.append(
                f"Root cause {correlation.get('verdict')} for {correlation.get('service_name', 'the service')} "
                f"(deploy {correlation.get('delta_minutes', '?')} min before the incident)"
            )
        runbook = verified.get("runbook")
        if runbook:
            parts.append(f"runbook {runbook.get('id')}")
        remediation = verified.get("remediation")
        if remediation:
            checks = remediation.get("checks") or []
            passed = sum(1 for check in checks if check["status"] == "passed")
            parts.append(f"local preflight {passed}/{len(checks)} checks passed")
        if not parts:
            return "[Status] Investigation in progress."
        prefix = "[Resolution]" if state == "Resolved" else "[Status]"
        suffix = " Incident resolved; closing." if state == "Resolved" else ""
        return f"{prefix} {'; '.join(parts)}.{suffix}"

    def _sync_to_servicenow(self, existing: Dict[str, Any], changes: Dict[str, Any], work_note: str, state: str) -> bool:
        """Mirrors the update onto the real ServiceNow incident, if this ticket has one."""
        sys_id = existing.get("servicenow_sys_id")
        if not sys_id or not servicenow_client.is_configured():
            return True
        try:
            servicenow_client.update_incident(sys_id, work_note, resolve=state == "Resolved")
            self.store.append_audit_event(
                "connector.servicenow.update", "ticket", existing["number"],
                fingerprint=existing.get("alert_fingerprint"), session_id=existing.get("session_id"),
                details={"connector": "servicenow", "outcome": "success", "to_state": "Resolved" if state == "Resolved" else existing.get("state")},
            )
            return True
        except servicenow_client.ServiceNowError as exc:
            logger.warning("ServiceNow update failed, local ticket still updated: %s", exc)
            changes["append_work_notes"].append(f"ServiceNow sync failed: {exc}")
            self.store.append_audit_event(
                "connector.servicenow.update", "ticket", existing["number"],
                fingerprint=existing.get("alert_fingerprint"), session_id=existing.get("session_id"),
                details={"connector": "servicenow", "outcome": "failure", "to_state": "Resolved" if state == "Resolved" else existing.get("state")},
            )
            return False

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
