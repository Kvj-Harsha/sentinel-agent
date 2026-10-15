"""Streamlit frontend for Sentinel."""

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Optional

import requests

# backend/run.py sets up the environment (.env, working directory, neuro-san paths); make it importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import streamlit as st  # noqa: E402  pylint: disable=wrong-import-position
from alerts import compose_incident_report  # noqa: E402  pylint: disable=wrong-import-position
from alerts import list_presets  # noqa: E402  pylint: disable=wrong-import-position
from alerts import load_preset  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import snapshot_jobs  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import start_receiver  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.ticket_store import TicketStore  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import github_pr_client  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import servicenow_client  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import ticket_notes  # noqa: E402  pylint: disable=wrong-import-position
from run import format_verified_facts  # noqa: E402  pylint: disable=wrong-import-position
from run import invoke_agent  # noqa: E402  pylint: disable=wrong-import-position

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


def reset_incident() -> None:
    """Starts a fresh incident: clears the conversation, its context and the ticket binding."""
    st.session_state.messages = []
    st.session_state.chat_context = {}
    st.session_state.sly_data = {}
    st.session_state.ticket_number = None


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


def _record_pr_ticket_update(ticket: Dict[str, Any], note: str, resolve: bool = False) -> None:
    """Record the human-reviewed PR outcome locally and on the linked ServiceNow incident."""
    number = ticket.get("number")
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changes: Dict[str, Any] = {"append_work_notes": [f"{now}: {note}"], "sys_updated_on": now}
    if resolve:
        changes["state"] = "Resolved"
    sys_id = ticket.get("servicenow_sys_id")
    if sys_id and servicenow_client.is_configured():
        servicenow_client.update_incident(sys_id, note, resolve=resolve, close_notes=note if resolve else "")
    TicketStore().update(number, changes)


@st.cache_data(ttl=12, show_spinner=False)
def github_pr_snapshot(number: int) -> tuple:
    """Fetch one cached PR snapshot so the live alert panel does not exhaust GitHub API limits."""
    pr = github_pr_client.get_pull_request(number)
    checks = github_pr_client.get_check_runs(pr["head"]["sha"])
    diff = github_pr_client.get_pull_request_diff(number)
    issue_number = github_pr_client.linked_issue_number(pr)
    issue = github_pr_client.get_issue(issue_number) if issue_number else None
    return pr, checks, diff, issue


def _finalize_merged_pr(ticket: Dict[str, Any], pr_number: int) -> None:
    """Apply the reviewed settings locally, verify checkout, and resolve the linked ticket."""
    config_path = github_pr_client.sync_merged_settings()
    app_url = os.environ.get("SENTINEL_DEMO_APP_URL", "http://127.0.0.1:8000").rstrip("/")
    requests.post(f"{app_url}/demo/recover", timeout=5).raise_for_status()
    recovery = requests.post(f"{app_url}/checkout", json={"order_id": "sentinel-recovery-check"}, timeout=5)
    recovery.raise_for_status()
    note = (
        f"[Human approval] GitHub PR #{pr_number} merged after all Actions checks passed. "
        f"Merged config synced to the local demo app ({config_path}); checkout recovery verified."
    )
    _record_pr_ticket_update(ticket, note, resolve=True)


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
        st.markdown(f"Demo app: [Open checkout service]({os.environ.get('SENTINEL_DEMO_APP_URL', 'http://127.0.0.1:8000')})")
        if not github_pr_client.is_configured():
            st.warning("Connect the dedicated demo GitHub repository to enable PR creation and review.")
            st.caption("Set SENTINEL_GITHUB_TOKEN, SENTINEL_DEMO_GITHUB_OWNER and SENTINEL_DEMO_GITHUB_REPO in .env.")
            return

        pr_state_key = f"sentinel_pr_{ticket['number']}"
        pr_number = st.session_state.get(pr_state_key)
        if not pr_number:
            st.markdown("**Proposed change:** disable the injected checkout fault in `app/settings.json`.")
            st.caption(f"Repository: {github_pr_client.repository_name()} · app: localhost demo only")
            if st.button("Prepare repair PR", type="primary", key=f"create_pr_{instance_key}_{ticket['number']}"):
                try:
                    pr = github_pr_client.create_repair_pr(ticket["number"])
                    st.session_state[pr_state_key] = pr["number"]
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
                    st.error(str(exc))
            return

        try:
            pr, checks, diff, issue = github_pr_snapshot(int(pr_number))
        except (github_pr_client.GithubPullRequestError, KeyError, ValueError) as exc:
            st.error(f"Could not load the pull request: {exc}")
            return

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
            if st.button("Verify recovery and update ticket", type="primary", key=f"finalize_pr_{ticket['number']}"):
                try:
                    _finalize_merged_pr(ticket, int(pr_number))
                    github_pr_snapshot.clear()
                    st.success(f"Checkout recovered. Ticket {ticket['number']} resolved.")
                    st.rerun()
                except (github_pr_client.GithubPullRequestError, requests.RequestException,
                        servicenow_client.ServiceNowError, OSError, ValueError) as exc:
                    st.error(f"Recovery or ticket sync needs attention: {exc}")
            return
        approved = st.checkbox(
            "I reviewed this diff and the GitHub Actions results; I approve merging this change.",
            key=f"human_approve_{ticket['number']}",
        )
        can_merge = approved and github_pr_client.checks_passed(checks) and pr.get("state") == "open"
        if st.button("Approve and merge repair", type="primary", disabled=not can_merge,
                     key=f"merge_pr_{ticket['number']}"):
            try:
                merged = github_pr_client.merge_pull_request(int(pr_number), pr["head"]["sha"])
                if not merged.get("merged"):
                    raise github_pr_client.GithubPullRequestError(merged.get("message", "GitHub did not merge the PR."))
                github_pr_snapshot.clear()
                _finalize_merged_pr(ticket, int(pr_number))
                st.success(f"PR merged and checkout recovered. Ticket {ticket['number']} resolved.")
                st.rerun()
            except (github_pr_client.GithubPullRequestError, requests.RequestException,
                    servicenow_client.ServiceNowError, OSError, ValueError) as exc:
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
        pending = compose_incident_report(alert)
    if reset_col.button("New incident", use_container_width=True):
        reset_incident()

st.caption("Logs, deploys, runbooks and pipeline fixtures are demo data; tickets are real when ServiceNow is configured.")


@st.fragment(run_every=2)
def render_alertmanager_activity() -> None:
    """Polls the local webhook receiver and shows alerts that arrived outside the chat UI."""
    jobs = snapshot_jobs()
    with st.container(border=True):
        st.subheader("Prometheus · Alertmanager")
        st.caption("Webhook: POST /alerts/alertmanager · receiver port 8090 · polling every 2 seconds")
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

ticket_slot = st.empty()
with ticket_slot.container():
    render_ticket(current_ticket())

verified = format_verified_facts(st.session_state.sly_data)
if verified:
    with st.expander("Verified by tools", expanded=True):
        st.markdown(verified)
        st.caption("These facts come from deterministic tools, not from the LLM's wording of the answer.")

render_human_approval(current_ticket())

st.divider()
st.subheader("Incident investigation")
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

typed = st.chat_input("Ask about this incident or describe a new one...")
if typed:
    pending = typed

if pending:
    with st.chat_message("user"):
        st.markdown(pending)
    with st.chat_message("assistant"):
        with st.spinner("Commander is coordinating the agents (about a minute)..."):
            run_turn_live(pending, ticket_slot)
    st.rerun()
