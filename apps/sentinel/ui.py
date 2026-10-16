"""Streamlit frontend for Sentinel."""

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Optional
from urllib.parse import urlparse

import requests

# run.py (next to this file) sets up the environment (.env, working directory, neuro-san paths).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import streamlit as st  # noqa: E402  pylint: disable=wrong-import-position
from alerts import compose_incident_report  # noqa: E402  pylint: disable=wrong-import-position
from alerts import list_presets  # noqa: E402  pylint: disable=wrong-import-position
from alerts import load_preset  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import snapshot_jobs  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import start_receiver  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import receiver_status  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.ticket_store import TicketStore  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import github_pr_client  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import servicenow_client  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.schemas import redact_text  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import ticket_notes  # noqa: E402  pylint: disable=wrong-import-position
from run import invoke_agent  # noqa: E402  pylint: disable=wrong-import-position

if os.environ.get("SENTINEL_RECEIVER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}:
    start_receiver()

POLL_SECONDS = 1.5

st.set_page_config(page_title="Sentinel", layout="wide", initial_sidebar_state="collapsed")
st.markdown(
    """
    <style>
    .block-container { max-width: 900px; padding: 1.25rem 1rem 5rem; }
    @media (max-width: 640px) {
      .block-container { padding: 1rem 0.75rem 4rem; }
      [data-testid="stMarkdownContainer"] p { overflow-wrap: anywhere; }
    }
    [data-testid="stChatMessage"] { border-radius: 0.75rem; }
    [data-testid="stExpander"] { border-radius: 0.6rem; }
    [data-testid="stVerticalBlockBorderWrapper"] { border-radius: 0.8rem; }
    [data-testid="stLinkButton"] { width: 100%; }
    </style>
    """,
    unsafe_allow_html=True,
)

if "messages" not in st.session_state:
    st.session_state.messages = []
if "chat_context" not in st.session_state:
    st.session_state.chat_context = {}
if "sly_data" not in st.session_state:
    st.session_state.sly_data = {}
if "ticket_number" not in st.session_state:
    st.session_state.ticket_number = None
if "alert_context" not in st.session_state:
    st.session_state.alert_context = None


def reset_incident() -> None:
    """Starts a fresh incident: clears the conversation, its context and the ticket binding."""
    st.session_state.messages = []
    st.session_state.chat_context = {}
    st.session_state.sly_data = {}
    st.session_state.ticket_number = None
    st.session_state.alert_context = None


SOURCE_GROUPS = {
    "Metrics": {"metric"},
    "Logs": {"log"},
    "Deployments": {"deployment", "deployment_change", "commit"},
    "Runbooks": {"runbook"},
    "Incident memory": set(),
}


def _safe_http_url(value: Any) -> bool:
    """Only offer ordinary web links; never render provider-controlled URL text as Markdown."""
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password


def render_alert_context() -> None:
    """Show the alert's origin and time context separately from agent-written analysis."""
    context = st.session_state.get("alert_context")
    if not context:
        return
    with st.container(border=True):
        st.subheader("Alert context · source input")
        title = context.get("title") or context.get("origin") or "Alert"
        st.write(title)
        left, right = st.columns(2)
        left.write(f"Service: {context.get('service', 'not identified')}")
        right.write(f"Environment: {context.get('environment', 'not identified')}")
        left.write(f"Severity: {context.get('severity', 'not provided')}")
        right.write(f"Event time: {context.get('event_time', 'not provided')}")
        st.caption(f"Origin: {context.get('origin', 'free-text input')}")


def render_verified_evidence(sly_data: Optional[Dict[str, Any]]) -> None:
    """Render provenance-backed tool outputs and deterministic verdict apart from LLM prose."""
    verified = (sly_data or {}).get("sentinel_verified") or {}
    evidence = verified.get("evidence") or []
    if not verified and not evidence:
        return
    st.subheader("Evidence and source status · deterministic tools")
    correlation = verified.get("correlation") or {}
    verdict = correlation.get("verdict")
    if verdict:
        display_verdict = "INSUFFICIENT EVIDENCE" if correlation.get("insufficient_evidence") else verdict
        message = correlation.get("reasoning") or "No deterministic explanation was recorded."
        if display_verdict == "SUPPORTED":
            st.success(f"Correlation verdict: {display_verdict} · {message}")
        elif verdict in {"NOT_SUPPORTED", "UNSUPPORTED"}:
            if correlation.get("insufficient_evidence"):
                st.info(f"Correlation verdict: {display_verdict} · {message}")
            else:
                st.warning(f"Correlation verdict: {display_verdict} · {message}")
        else:
            st.info(f"Correlation verdict: {display_verdict} · {message}")
        st.caption(
            f"Incident time: {correlation.get('incident_time', 'unknown')} · "
            f"Deployment time: {correlation.get('deploy_time', 'unknown')} · "
            f"Delta: {correlation.get('delta_minutes', 'unknown')} min"
        )

    deploy_lookup = verified.get("deployment_lookup") or {}
    runbook = verified.get("runbook") or {}
    memory = verified.get("memory") or {}
    grouped: Dict[str, list] = {name: [] for name in SOURCE_GROUPS}
    for record in evidence:
        for category, types in SOURCE_GROUPS.items():
            if record.get("evidence_type") in types:
                grouped[category].append(record)
                break

    with st.expander("Source results", expanded=True):
        columns = st.columns(2)
        for index, (category, records) in enumerate(grouped.items()):
            with columns[index % 2]:
                st.markdown(f"**{category}**")
                if records:
                    statuses = sorted({str(item.get("source_status", "unknown")).upper() for item in records})
                    st.caption(f"Status: {' / '.join(statuses)} · {len(records)} record(s)")
                    for record_index, item in enumerate(records):
                        st.write(item.get("summary") or item.get("evidence_type", "Evidence record"))
                        st.caption(
                            f"Source: {item.get('source', 'unknown')} · ID: {item.get('record_id') or 'not provided'} · "
                            f"Event: {item.get('event_time') or 'time unavailable'} · "
                            f"Retrieved: {item.get('retrieved_at') or 'time unavailable'}"
                        )
                        safe_fields = item.get("safe_fields") or {}
                        if safe_fields.get("window_start") or safe_fields.get("window_end"):
                            st.caption(
                                f"Query window: {safe_fields.get('window_start', 'unknown')} → "
                                f"{safe_fields.get('window_end', 'unknown')}"
                            )
                        if _safe_http_url(item.get("source_url")):
                            st.link_button(
                                "Open source", item["source_url"], use_container_width=True,
                                key=f"evidence_source_{category}_{record_index}",
                            )
                elif category == "Deployments" and deploy_lookup.get("source_status"):
                    status = str(deploy_lookup.get("source_status", "unknown")).upper()
                    st.caption(f"Status: {status} · {deploy_lookup.get('source', 'deployment source')}")
                    st.write("No deployment evidence was published for this investigation.")
                elif category == "Runbooks" and runbook:
                    st.caption("Status: MATCHED")
                    st.write(f"{runbook.get('id', 'Runbook')}: {runbook.get('title', '')}")
                    if _safe_http_url(runbook.get("webui_link")):
                        st.link_button(
                            "Open runbook", runbook["webui_link"], use_container_width=True,
                            key="evidence_runbook_source",
                        )
                elif category == "Incident memory" and memory:
                    st.caption("Status: LOCAL HISTORY")
                    st.write(f"Prior incidents found: {memory.get('prior_incident_count', 0)}")
                    if memory.get("suggested_hotfix"):
                        st.write(f"Suggested from history: {memory['suggested_hotfix']}")
                else:
                    st.caption("Status: NO EVIDENCE PUBLISHED")
                    st.write("No verified records were published for this source in this investigation.")
        st.caption("A configured connector is not proof of a successful query. Status here reflects recorded results only.")


def load_tickets() -> list:
    """:return: every stored ticket; tolerant of the file being mid-write while an agent updates it."""
    try:
        return TicketStore().load()
    except ValueError:
        return []


def current_ticket(known_before: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    :param known_before: while a run is in flight, the ticket count before it started; any ticket past that
        count is the one this run opened (the conversation's sly_data is only returned when the run ends).
    :return: the ticket bound to this conversation (else the most recent one), or None.
    """
    tickets = load_tickets()
    if known_before is not None:
        if len(tickets) > known_before:
            st.session_state.ticket_number = tickets[-1].get("number")
            return tickets[-1]
        return None
    number = st.session_state.ticket_number or st.session_state.sly_data.get("sentinel_ticket_number")
    for ticket in tickets:
        if ticket.get("number") == number:
            return ticket
    return None


def render_ticket(ticket: Optional[Dict[str, Any]], running: bool = False) -> None:
    """Draws a full-width incident card with the ServiceNow link, ownership and expandable activity."""
    st.subheader("Incident ticket")
    if ticket is None:
        st.info("Ticket opens as soon as the TicketAgent runs..." if running else
                "No ticket yet. Fire an alert above and the TicketAgent opens one.")
        return

    with st.container(border=True):
        st.markdown(f"### {ticket['number']} · {ticket['state']}")
        if ticket.get("servicenow_url"):
            st.markdown(f"🔗 **[Open {ticket['number']} in ServiceNow]({ticket['servicenow_url']})** · Live record")
        else:
            st.caption("Local demo ticket · set SERVICENOW_* in .env to create real incidents")
        left, right = st.columns(2, gap="medium")
        left.markdown(f"**Service**  \n{ticket['service_name']}")
        right.markdown(f"**Priority**  \n{ticket['priority']}")
        left.markdown(f"**Caller**  \n{ticket.get('caller_id', '-')}")
        right.markdown(f"**Assignment group**  \n{ticket.get('assignment_group', '-')}")
        left.markdown(f"**Assigned to**  \n{ticket.get('assigned_to', '-')}")
        notes = ticket.get("work_notes", [])
        if notes:
            with st.expander(f"Ticket activity · {len(notes)} updates", expanded=running):
                for note in notes:
                    first_line, _, rest = note.partition("\n")
                    st.markdown(f"- {first_line}")
                    if rest:
                        st.code(rest, language="diff")


def _record_pr_ticket_update(ticket: Dict[str, Any], note: str, resolve: bool = False, pr_number: Optional[int] = None) -> None:
    """Record the human-reviewed PR outcome locally and on the linked ServiceNow incident."""
    number = ticket.get("number")
    note = redact_text(note)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changes: Dict[str, Any] = {"append_work_notes": [f"{now}: {note}"], "sys_updated_on": now}
    if pr_number is not None:
        changes["repair_pr_number"] = str(pr_number)
    if resolve:
        changes["state"] = "Resolved"
    sys_id = ticket.get("servicenow_sys_id")
    store = TicketStore()
    if sys_id and servicenow_client.is_configured():
        try:
            servicenow_client.update_incident(sys_id, note, resolve=resolve, close_notes=note if resolve else "")
            store.append_audit_event(
                "connector.servicenow.recovery_update", "ticket", number,
                fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                details={"connector": "servicenow", "outcome": "success", "to_state": "Resolved" if resolve else ticket.get("state")},
            )
        except servicenow_client.ServiceNowError:
            store.append_audit_event(
                "connector.servicenow.recovery_update", "ticket", number,
                fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                details={"connector": "servicenow", "outcome": "failure", "to_state": "Resolved" if resolve else ticket.get("state")},
            )
            raise
    store.update(number, changes)


@st.cache_data(ttl=12, show_spinner=False)
def github_pr_snapshot(number: int) -> tuple:
    """Fetch one cached PR snapshot so the live alert panel does not exhaust GitHub API limits."""
    pr = github_pr_client.get_pull_request(number)
    checks = github_pr_client.get_check_runs(pr["head"]["sha"])
    diff = github_pr_client.get_pull_request_diff(number)
    files = github_pr_client.get_pull_request_files(number)
    issue_number = github_pr_client.linked_issue_number(pr)
    issue = github_pr_client.get_issue(issue_number) if issue_number else None
    return pr, checks, diff, issue, files


def _finalize_merged_pr(ticket: Dict[str, Any], pr_number: int, expected_sha: str) -> None:
    """Apply the reviewed settings locally, verify checkout, and resolve the linked ticket."""
    repair_context = ticket.get("repair_context") or {}
    config_path = github_pr_client.sync_merged_settings(
        pr_number, ticket["number"], repair_context, ticket.get("session_id", ""), expected_sha
    )
    app_url = os.environ.get("SENTINEL_DEMO_APP_URL", "http://127.0.0.1:8000").rstrip("/")
    requests.post(f"{app_url}/demo/recover", timeout=5).raise_for_status()
    recovery = requests.post(f"{app_url}/checkout", json={"order_id": "sentinel-recovery-check"}, timeout=5)
    recovery.raise_for_status()
    note = (
        f"[Human approval] GitHub PR #{pr_number} merged after all Actions checks passed. "
        f"Merged config synced to the local demo app ({config_path}); checkout recovery verified."
    )
    _record_pr_ticket_update(ticket, note, resolve=True, pr_number=pr_number)


def render_human_approval(ticket: Optional[Dict[str, Any]], instance_key: str = "main") -> None:
    """Show the live GitHub PR diff/checks and require an explicit human merge decision."""
    with st.container(border=True):
        st.subheader("Human review · repair pull request")
        if ticket is None:
            st.info("Investigate an alert first. Sentinel will prepare a repair PR for the demo checkout service.")
            return
        if ticket.get("service_name") != "checkout-service":
            st.info("The repair PR workflow is currently scoped to the localhost checkout demo service.")
            return
        repair_context = ticket.get("repair_context") or {}
        if (
            not repair_context.get("eligible")
            or repair_context.get("ticket_number") != ticket.get("number")
            or repair_context.get("session_id") != ticket.get("session_id")
            or repair_context.get("service_name") != ticket.get("service_name")
            or repair_context.get("verdict") != "SUPPORTED"
            or not repair_context.get("all_passed")
        ):
            st.info("Repair creation is locked until this ticket has a matching SUPPORTED correlation, log and deployment evidence, and passing local preflight checks.")
            return
        st.markdown(f"Demo app: [Open checkout service]({os.environ.get('SENTINEL_DEMO_APP_URL', 'http://127.0.0.1:8000')})")
        if not github_pr_client.is_configured():
            st.warning("Connect the dedicated demo GitHub repository to enable PR creation and review.")
            st.caption("Set SENTINEL_GITHUB_TOKEN, SENTINEL_DEMO_GITHUB_OWNER and SENTINEL_DEMO_GITHUB_REPO in .env.")
            return

        pr_state_key = f"sentinel_pr_{ticket['number']}"
        pr_number = st.session_state.get(pr_state_key) or ticket.get("repair_pr_number")
        if not pr_number:
            st.markdown("**Proposed change:** disable the injected checkout fault in `app/settings.json`.")
            st.caption(f"Repository: {github_pr_client.repository_name()} · app: localhost demo only")
            if st.button("Prepare repair PR", type="primary", key=f"create_pr_{instance_key}_{ticket['number']}"):
                try:
                    pr = github_pr_client.create_repair_pr(ticket["number"], repair_context, ticket.get("session_id", ""))
                    st.session_state[pr_state_key] = pr["number"]
                    store = TicketStore()
                    store.update(ticket["number"], {"repair_pr_number": str(pr["number"]), "sys_updated_on": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                    store.append_audit_event(
                        "repair.pr_created", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "outcome": "success", "repair_pr_number": str(pr["number"])},
                    )
                    issue_note = (
                        f"GitHub issue #{pr['sentinel_issue_number']} created first: {pr['sentinel_issue_url']}. "
                        if pr.get("sentinel_issue_number") else ""
                    )
                    ticket_notes.add_note(
                        {"sentinel_ticket_number": ticket["number"]},
                        f"[GitHub] {issue_note}Repair PR #{pr['number']} opened for human review: {pr['html_url']}",
                    )
                    st.rerun()
                except github_pr_client.GithubPullRequestError as exc:
                    TicketStore().append_audit_event(
                        "connector.github.pr_create", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "connector": "github", "outcome": "failure"},
                    )
                    st.error(str(exc))
            return

        try:
            pr, checks, diff, issue, files = github_pr_snapshot(int(pr_number))
        except (github_pr_client.GithubPullRequestError, KeyError, ValueError) as exc:
            st.error(f"Could not load the pull request: {exc}")
            return

        scope_error = ""
        try:
            github_pr_client.verify_repair_pr(
                pr, files, ticket["number"], repair_context, ticket.get("session_id", ""),
                require_open=not pr.get("merged"),
            )
        except github_pr_client.GithubPullRequestError as exc:
            scope_error = str(exc)
            st.error(f"Approval is locked: {scope_error}")

        pr_col, checks_col = st.columns([1, 1])
        pr_col.markdown(f"**[{pr['title']}]({pr['html_url']})** · #{pr['number']} · {pr['state']}")
        pr_col.caption(f"{pr['head']['ref']} → {pr['base']['ref']} · commit {pr['head']['sha'][:7]}")
        issue_number = github_pr_client.linked_issue_number(pr)
        if issue_number:
            issue_url = issue.get("html_url") if issue else f"https://github.com/{github_pr_client.repository_name()}/issues/{issue_number}"
            issue_state = issue.get("state", "unknown").upper() if issue else "unavailable"
            st.markdown(f"**Linked GitHub issue:** [#{issue_number}]({issue_url}) · {issue_state}")
            if pr.get("merged"):
                if issue_state == "CLOSED":
                    st.success("The linked GitHub issue closed when this PR was merged.")
                else:
                    st.warning("The PR is merged, but GitHub has not reported the linked issue as closed yet.")
            else:
                st.caption("GitHub will close this issue only when the reviewed PR is merged.")
        else:
            st.warning("This PR has no linked GitHub issue yet.")
        if pr.get("merged"):
            checks_col.success("Merged")
        elif not checks:
            checks_col.info("GitHub Actions checks are starting…")
        elif github_pr_client.checks_passed(checks):
            checks_col.success(f"All {len(checks)} check(s) passed")
        elif any(check.get("conclusion") == "failure" for check in checks):
            checks_col.error("A GitHub check failed. Review the run before proceeding.")
        else:
            checks_col.warning("Checks are still running or need attention")

        with st.expander("Review the actual GitHub diff", expanded=True):
            st.code(diff or "No textual diff returned by GitHub.", language="diff")

        for check in checks:
            conclusion = check.get("conclusion") or check.get("status", "unknown")
            label = "✅" if conclusion == "success" else "❌" if conclusion in ("failure", "cancelled", "timed_out") else "⏳"
            check_url = check.get("details_url")
            st.markdown(f"{label} **{check.get('name', 'Check')}** · {conclusion}" +
                        (f" · [view run]({check_url})" if check_url else ""))

        if pr.get("merged"):
            if ticket.get("state") == "Resolved":
                st.success("PR merged, recovery verified, and the incident is resolved.")
                return
            st.warning("PR is merged; finish syncing the demo app and incident record.")
            if st.button("Verify recovery and update ticket", type="primary", disabled=bool(scope_error), key=f"finalize_pr_{ticket['number']}"):
                try:
                    TicketStore().append_audit_event(
                        "repair.finalize", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "decision": "verify_recovery", "outcome": "attempted", "repair_pr_number": str(pr_number)},
                    )
                    _finalize_merged_pr(ticket, int(pr_number), pr["head"]["sha"])
                    TicketStore().append_audit_event(
                        "repair.finalize", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "decision": "verify_recovery", "outcome": "recovery_verified", "repair_pr_number": str(pr_number)},
                    )
                    github_pr_snapshot.clear()
                    st.success(f"Checkout recovered. Ticket {ticket['number']} resolved.")
                    st.rerun()
                except (github_pr_client.GithubPullRequestError, requests.RequestException,
                        servicenow_client.ServiceNowError, OSError, ValueError) as exc:
                    TicketStore().append_audit_event(
                        "repair.finalize", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "decision": "verify_recovery", "outcome": "failure", "repair_pr_number": str(pr_number)},
                    )
                    st.error(f"Recovery or ticket sync needs attention: {exc}")
            return
        approved = st.checkbox(
            "I reviewed this diff and the GitHub Actions results; I approve merging this change.",
            key=f"human_approve_{ticket['number']}_{pr['head']['sha']}",
        )
        can_merge = approved and github_pr_client.checks_passed(checks) and pr.get("state") == "open" and not scope_error
        if st.button("Approve and merge repair", type="primary", disabled=not can_merge,
                     key=f"merge_pr_{ticket['number']}_{pr['head']['sha']}"):
            merge_completed = False
            try:
                TicketStore().append_audit_event(
                    "human.approval", "ticket", ticket["number"],
                    fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                    details={"actor": "streamlit-user", "decision": "approve_merge", "outcome": "requested", "repair_pr_number": str(pr_number)},
                )
                merged = github_pr_client.merge_pull_request(
                    int(pr_number), pr["head"]["sha"], ticket["number"], repair_context, ticket.get("session_id", "")
                )
                if not merged.get("merged"):
                    raise github_pr_client.GithubPullRequestError(merged.get("message", "GitHub did not merge the PR."))
                merge_completed = True
                TicketStore().append_audit_event(
                    "human.approval", "ticket", ticket["number"],
                    fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                    details={"actor": "streamlit-user", "decision": "approve_merge", "outcome": "success", "repair_pr_number": str(pr_number)},
                )
                github_pr_snapshot.clear()
                TicketStore().append_audit_event(
                    "repair.finalize", "ticket", ticket["number"],
                    fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                    details={"actor": "streamlit-user", "decision": "verify_recovery", "outcome": "attempted", "repair_pr_number": str(pr_number)},
                )
                _finalize_merged_pr(ticket, int(pr_number), pr["head"]["sha"])
                TicketStore().append_audit_event(
                    "repair.finalize", "ticket", ticket["number"],
                    fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                    details={"actor": "streamlit-user", "decision": "verify_recovery", "outcome": "recovery_verified", "repair_pr_number": str(pr_number)},
                )
                st.success(f"PR merged and checkout recovered. Ticket {ticket['number']} resolved.")
                st.rerun()
            except (github_pr_client.GithubPullRequestError, requests.RequestException,
                    servicenow_client.ServiceNowError, OSError, ValueError) as exc:
                if not merge_completed:
                    TicketStore().append_audit_event(
                        "human.approval", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "decision": "approve_merge", "outcome": "failure", "repair_pr_number": str(pr_number)},
                    )
                if merge_completed:
                    TicketStore().append_audit_event(
                        "repair.finalize", "ticket", ticket["number"],
                        fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
                        details={"actor": "streamlit-user", "decision": "verify_recovery", "outcome": "failure", "repair_pr_number": str(pr_number)},
                    )
                st.error(f"The approval workflow stopped safely: {exc}")


def run_turn_live(text: str, ticket_slot: Any) -> None:
    """
    Sends one message to the agent network on a worker thread and, while it runs, keeps redrawing the full-width
    ticket card so the audience sees the ticket open and its notes arrive in real time.

    :param text: the user message / composed incident report.
    :param ticket_slot: a Streamlit placeholder to redraw the ticket card into.
    """
    st.session_state.messages.append({"role": "user", "content": text})
    sly_data = dict(st.session_state.sly_data)
    chat_context = dict(st.session_state.chat_context)
    known_before = len(load_tickets()) if not sly_data.get("sentinel_ticket_number") else None
    outcome: Dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["response"] = invoke_agent(text, sly_data=sly_data, chat_context=chat_context)
        except Exception as exc:  # pylint: disable=broad-exception-caught
            outcome["error"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    while thread.is_alive():
        with ticket_slot.container():
            render_ticket(current_ticket(known_before) if known_before is not None else current_ticket(), True)
        time.sleep(POLL_SECONDS)
    thread.join()

    if "error" in outcome:
        st.session_state.messages.append({"role": "assistant", "content": f"Error: {outcome['error']}"})
        return
    response = outcome.get("response", {})
    st.session_state.messages.append({"role": "assistant", "content": response.get("text", "(no response)")})
    if response.get("chat_context"):
        st.session_state.chat_context = response["chat_context"]
    if response.get("sly_data"):
        st.session_state.sly_data = response["sly_data"]


st.title("Sentinel")
st.caption("Incident response with evidence, a reviewable fix, and human approval before recovery.")

pending: Optional[str] = None
with st.container(border=True):
    st.subheader("Start with an alert")
    preset = st.selectbox("Sample monitoring alert", list_presets())
    alert = load_preset(preset)
    with st.expander("View alert payload"):
        st.json(alert)
    fire_col, reset_col = st.columns([3, 1], gap="small")
    if fire_col.button("🚨  Investigate alert", type="primary", use_container_width=True):
        reset_incident()
        tags = alert.get("tags") or []
        labels = {}
        for tag in tags:
            if isinstance(tag, str) and ":" in tag:
                key, value = tag.split(":", 1)
                labels[key.strip().lower()] = value.strip()
        st.session_state.alert_context = {
            "origin": f"Sample alert · {preset}",
            "title": alert.get("title") or alert.get("alert_type") or "Sample alert",
            "service": labels.get("service") or alert.get("service") or "not identified",
            "environment": labels.get("env") or alert.get("environment") or "not identified",
            "severity": alert.get("severity") or "not provided",
            "event_time": alert.get("date") or alert.get("fired_at") or "not provided",
        }
        pending = compose_incident_report(alert)
    if reset_col.button("New incident", use_container_width=True):
        reset_incident()

with st.expander("Data sources and demo scope"):
    st.markdown(
        """
        - **Alert input · local/demo:** sample alerts and free-text input; the optional Prometheus/Alertmanager stack runs locally.
        - **Metrics and logs:** local Prometheus/Loki queries when their coded tools return evidence; fixture results are labeled separately.
        - **Deployments:** explicit GitHub deployment records when configured; fixture/change-history records keep their own provenance, and a commit is not proof of a deployment.
        - **Runbooks:** Confluence results when configured, otherwise local fixture results; provenance is shown per record.
        - **Incident memory:** local incident history only.
        - **Tickets · local SQLite**, with an optional ServiceNow mirror when configured.
        - **Repair PR and checks · optional GitHub integration:** limited to the configured demo repository.
        - **Recovery · localhost demo only:** Sentinel applies only the allow-listed demo setting; it does not deploy to production.
        """
    )
    st.caption(
        f"ServiceNow mirror: {'configured' if servicenow_client.is_configured() else 'not configured'} · "
        f"GitHub demo PR: {'configured' if github_pr_client.is_configured() else 'not configured'}"
    )


@st.fragment(run_every=2)
def render_alertmanager_activity() -> None:
    """Polls the local webhook receiver and shows alerts that arrived outside the chat UI."""
    jobs = snapshot_jobs()
    with st.container(border=True):
        st.subheader("Prometheus · Alertmanager")
        st.caption("Webhook: POST /alerts/alertmanager · receiver port 8090 · polling every 2 seconds")
        status_url = os.environ.get("SENTINEL_WEBHOOK_STATUS_URL", "").strip()
        if status_url:
            try:
                response = requests.get(status_url, timeout=2)
                response.raise_for_status()
                health = {"status": "listening", "host": "sentinel-webhook", "port": 8090}
            except requests.RequestException:
                health = {"status": "unavailable"}
        else:
            health = receiver_status()
        if health["status"] == "listening":
            st.success(f"Sentinel webhook receiver listening on {health['host']}:{health['port']}")
        elif status_url:
            st.warning("Sentinel webhook service is unavailable or still starting.")
        else:
            st.error("Webhook receiver is disabled. Configure a 24+ character SENTINEL_WEBHOOK_TOKEN and restart Sentinel.")
        if not jobs:
            st.info("Waiting for an Alertmanager firing alert. The Prometheus demo stack can send one automatically.")
            return
        for job in jobs[:5]:
            state = job.get("state", "queued")
            with st.expander(
                f"{job.get('alert_name', 'Alert')} · {job.get('service', 'unknown service')} · {state.upper()}",
                expanded=job is jobs[0],
            ):
                st.caption(f"Alert {job.get('id')} · severity {job.get('severity', 'unknown')} · received {job.get('received_at')}")
                ticket = TicketStore().get(job.get("ticket_number")) if job.get("ticket_number") else None
                if ticket and job is jobs[0]:
                    st.session_state.ticket_number = ticket.get("number")
                if job.get("state") in ("queued", "investigating"):
                    st.info("Sentinel is investigating this webhook alert…")
                    if ticket:
                        render_ticket(ticket, running=True)
                elif job.get("error"):
                    st.error(job["error"])
                else:
                    if ticket:
                        render_ticket(ticket)
                        render_human_approval(ticket, str(job.get("id", "alert")))
                    if job.get("verified_markdown"):
                        st.markdown("**Verified evidence**")
                        st.markdown(job["verified_markdown"])
                    if job.get("response"):
                        st.markdown("**Agent investigation summary**")
                        st.markdown(job["response"])


render_alertmanager_activity()

render_alert_context()

ticket_slot = st.empty()
with ticket_slot.container():
    render_ticket(current_ticket())

render_verified_evidence(st.session_state.sly_data)

render_human_approval(current_ticket())

st.divider()
st.subheader("Incident investigation")
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        if message["role"] == "assistant":
            st.caption("Agent-generated response")
        st.markdown(message["content"])

typed = st.chat_input("Ask about this incident or describe a new one...")
if typed:
    pending = typed
    st.session_state.alert_context = {
        "origin": "Free-text user input",
        "title": "User-described incident",
        "service": "not identified from input metadata",
        "environment": "not provided",
        "severity": "not provided",
        "event_time": "not provided",
    }

if pending:
    with st.chat_message("user"):
        st.markdown(pending)
    with st.chat_message("assistant"):
        with st.spinner("Commander is coordinating the agents (about a minute)..."):
            run_turn_live(pending, ticket_slot)
    st.rerun()
