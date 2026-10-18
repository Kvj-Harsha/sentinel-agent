"""Checks for the private session-to-ticket binding before incident mutations."""
from typing import Any, Dict


def owns_ticket(ticket: Dict[str, Any], state: Dict[str, Any]) -> bool:
    return bool(ticket and state.get("incident_session_id")
                and ticket.get("session_id") == state.get("incident_session_id")
                and ticket.get("number") == state.get("sentinel_ticket_number"))


def job_owns_ticket(ticket: Dict[str, Any], job: Dict[str, Any]) -> bool:
    return bool(ticket and ticket.get("service_name") == job.get("service")
                and ((job.get("id") and ticket.get("session_id") == job["id"])
                     or (job.get("fingerprint") and ticket.get("alert_fingerprint") == job["fingerprint"])))


def checked_result(seed: Dict[str, Any], result: Dict[str, Any], store) -> Dict[str, Any]:
    """Retain private identity on partial results; reject unrelated agent results."""
    merged = {**seed, **result}
    number = merged.get("sentinel_ticket_number")
    if not number:
        if merged.get("incident_session_id") != seed.get("incident_session_id"):
            raise ValueError("Investigation session binding changed.")
        return merged
    ticket = store.get(number)
    same_session = merged.get("incident_session_id") == seed.get("incident_session_id")
    fingerprint_reuse = bool(seed.get("alert_fingerprint") and ticket
                             and ticket.get("alert_fingerprint") == seed.get("alert_fingerprint"))
    if not owns_ticket(ticket, merged) or not (same_session or fingerprint_reuse):
        raise ValueError("Investigation ticket does not belong to this session.")
    if seed.get("sentinel_ticket_number") and number != seed["sentinel_ticket_number"]:
        raise ValueError("Investigation ticket binding changed.")
    return merged
