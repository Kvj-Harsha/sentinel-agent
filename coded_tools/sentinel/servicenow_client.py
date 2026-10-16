"""Minimal client for the real ServiceNow Table API (incident table); optional, env-configured."""

import logging
import os
from typing import Any
from typing import Dict
from typing import Optional
from typing import Tuple

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20
STATE_IN_PROGRESS = "2"
STATE_RESOLVED = "6"
# A valid "Resolution code" choice on current releases; older ones use "Solved (Permanently)".
DEFAULT_CLOSE_CODE = "Solution provided"
STATE_LABELS = {"1": "New", "2": "In Progress", "3": "On Hold", "6": "Resolved", "7": "Closed", "8": "Canceled"}


class ServiceNowError(Exception):
    """Raised when the ServiceNow instance cannot be reached or rejects a request."""


def is_configured() -> bool:
    """:return: True when enabled and all required ServiceNow credentials are set."""
    if os.environ.get("SERVICENOW_ENABLED", "true").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    return all(os.environ.get(name) for name in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD"))


def base_url() -> str:
    """:return: the instance root, e.g. 'https://dev442258.service-now.com' (accepts a bare 'dev442258')."""
    instance = os.environ["SERVICENOW_INSTANCE"].strip().rstrip("/")
    if not instance.startswith("http"):
        instance = f"https://{instance}" if "." in instance else f"https://{instance}.service-now.com"
    return instance


def record_url(sys_id: str) -> str:
    """:return: the browser URL of an incident record."""
    return f"{base_url()}/nav_to.do?uri=incident.do?sys_id={sys_id}"


def _request(method: str, path: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        response = requests.request(
            method,
            f"{base_url()}/api/now/table/{path}",
            auth=(os.environ["SERVICENOW_USER"], os.environ["SERVICENOW_PASSWORD"]),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            json=payload,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException:
        raise ServiceNowError("ServiceNow is unreachable or the request failed.") from None
    if response.status_code >= 400:
        # Response bodies can echo submitted incident text or implementation details.
        raise ServiceNowError(f"ServiceNow returned HTTP {response.status_code}.")
    try:
        return response.json()["result"]
    except (ValueError, KeyError) as exc:
        raise ServiceNowError("ServiceNow returned an unexpected response (is the instance hibernating?)") from exc


# ServiceNow derives priority from impact x urgency (1/1 -> P1, 2/1 -> P2, 2/2 -> P3).
PRIORITY_TO_IMPACT_URGENCY = {"1": ("1", "1"), "2": ("2", "1"), "3": ("2", "2")}


def lookup_sys_id(table: str, field: str, value: str) -> str:
    """:return: the sys_id of the first record in `table` whose `field` equals `value`, else ''."""
    rows = _request("GET", f"{table}?sysparm_query={field}={value}&sysparm_fields=sys_id&sysparm_limit=1")
    return rows[0]["sys_id"] if rows else ""


def first_group_member(group: str) -> Tuple[str, str]:
    """:return: (sys_id, display name) of an active member of the group, or ('', '') if none."""
    rows = _request(
        "GET",
        f"sys_user_grmember?sysparm_query=group.name={group}^user.active=true^userISNOTEMPTY"
        "&sysparm_fields=user&sysparm_display_value=all&sysparm_limit=1",
    )
    if not rows:
        return "", ""
    user = rows[0]["user"]
    return user["value"], user["display_value"]


def create_incident(short_description: str, description: str, priority_level: str) -> Dict[str, Any]:
    """
    Creates an incident on the real instance, already In Progress, with caller, assignment group and assignee.

    The caller defaults to the integration user and the group to "Software"; override with SERVICENOW_CALLER and
    SERVICENOW_ASSIGNMENT_GROUP (a user_name / group name). The assignee defaults to an active member of that
    group, because instances commonly enforce that the assignee belongs to the assignment group (SERVICENOW_ASSIGNEE
    overrides it with a user_name). Unknown users or groups are left blank rather than failing the incident.

    :return: the created record ("number", "sys_id") plus the "caller", "assignee" and "group" used.
    """
    impact, urgency = PRIORITY_TO_IMPACT_URGENCY.get(priority_level, ("2", "2"))
    caller = os.environ.get("SERVICENOW_CALLER", os.environ["SERVICENOW_USER"])
    group = os.environ.get("SERVICENOW_ASSIGNMENT_GROUP", "Software")
    payload: Dict[str, Any] = {
        "short_description": short_description,
        "description": description,
        "impact": impact,
        "urgency": urgency,
        "state": STATE_IN_PROGRESS,
        "category": "software",
        "contact_type": "monitoring",
    }
    assignee = ""
    try:
        caller_id = lookup_sys_id("sys_user", "user_name", caller)
        group_id = lookup_sys_id("sys_user_group", "name", group)
        if os.environ.get("SERVICENOW_ASSIGNEE"):
            assignee = os.environ["SERVICENOW_ASSIGNEE"]
            assignee_id = lookup_sys_id("sys_user", "user_name", assignee)
        elif group_id:
            assignee_id, assignee = first_group_member(group)
        else:
            assignee_id = ""
    except ServiceNowError:
        caller_id = group_id = assignee_id = ""
    for field, value in (("caller_id", caller_id), ("assignment_group", group_id), ("assigned_to", assignee_id)):
        if value:
            payload[field] = value
    record = _request("POST", "incident", payload)
    record.update({"caller": caller, "assignee": assignee, "group": group})
    return record


def update_incident(
    sys_id: str, work_note: str, resolve: bool = False, close_notes: str = ""
) -> Dict[str, Any]:
    """
    Appends a work note and optionally resolves the incident (ServiceNow requires a close code and notes).

    :return: the updated record.
    """
    payload: Dict[str, Any] = {"work_notes": work_note}
    if resolve:
        payload.update(
            {
                "state": STATE_RESOLVED,
                "close_code": os.environ.get("SERVICENOW_CLOSE_CODE", DEFAULT_CLOSE_CODE),
                "close_notes": close_notes or work_note,
            }
        )
    return _request("PATCH", f"incident/{sys_id}", payload)
