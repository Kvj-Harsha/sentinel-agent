"""Posts progress notes onto the conversation's ticket (local store, and the real ServiceNow incident if any)."""

import logging
from datetime import datetime
from datetime import timezone
from typing import Any
from typing import Dict
from typing import Optional

from coded_tools.sentinel import servicenow_client
from coded_tools.sentinel.schemas import redact_text
from coded_tools.sentinel.ticket_store import TicketStore

logger = logging.getLogger(__name__)

TICKET_NUMBER_KEY = "sentinel_ticket_number"


def add_note(
    sly_data: Dict[str, Any], note: str, store: Optional[TicketStore] = None, once_key: str = ""
) -> bool:
    """
    Appends a work note to this conversation's ticket without changing its state.

    Best effort by design: investigation must never fail because a note could not be written, so a missing
    ticket or an unreachable ServiceNow instance only logs.

    :param sly_data: the conversation's sly_data (holds the ticket number once open_ticket has run).
    :param note: the note text.
    :param store: ticket store to use; defaults to the shared one.
    :param once_key: if given, the note is posted only once per conversation for this key (tools may be retried).
    :return: True if the note was recorded locally.
    """
    number = sly_data.get(TICKET_NUMBER_KEY)
    if not number:
        return False
    note = redact_text(note)
    posted = sly_data.setdefault("sentinel_notes_posted", [])
    if once_key and once_key in posted:
        return False
    if once_key:
        posted.append(once_key)
    store = store or TicketStore()
    ticket = store.get(number)
    if ticket is None:
        return False
    now = datetime.now(timezone.utc).isoformat()
    notes = [f"{now}: {note}"]
    sys_id = ticket.get("servicenow_sys_id")
    if sys_id and servicenow_client.is_configured():
        try:
            servicenow_client.update_incident(sys_id, note)
            store.append_audit_event(
                "connector.servicenow.work_note", "ticket", number,
                fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                details={"connector": "servicenow", "outcome": "success"},
            )
        except servicenow_client.ServiceNowError as exc:
            logger.warning("ServiceNow note failed (kept locally): %s", exc)
            notes.append(f"ServiceNow sync failed: {exc}")
            store.append_audit_event(
                "connector.servicenow.work_note", "ticket", number,
                fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                details={"connector": "servicenow", "outcome": "failure"},
            )
    store.update(number, {"append_work_notes": notes, "sys_updated_on": now})
    return True
