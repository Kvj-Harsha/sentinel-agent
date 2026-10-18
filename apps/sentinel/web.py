"""Sentinel web app: a dependency-free HTTP server that serves the single-page UI and its JSON API.

The investigation, evidence, ticket and repair logic all live in coded_tools/ and apps/sentinel/run.py; this module
only exposes them to the browser. The page itself is static (apps/sentinel/web/), so it needs no build step.
"""

import json
import logging
import mimetypes
import os
import socket
import sys
import threading
import time
import tracemalloc
import uuid
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from urllib.parse import urlparse

import requests

# run.py (next to this file) sets up the environment (.env, working directory, neuro-san paths).
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conversation_store  # noqa: E402  pylint: disable=wrong-import-position
from alerts import compose_incident_report  # noqa: E402  pylint: disable=wrong-import-position
from alerts import list_presets  # noqa: E402  pylint: disable=wrong-import-position
from alerts import load_preset  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import receiver_status  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import snapshot_jobs  # noqa: E402  pylint: disable=wrong-import-position
from alertmanager_receiver import start_receiver  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import github_pr_client  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import servicenow_client  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel import ticket_notes  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.confluence_cache import cache_stats  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.mode import demo_mode  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.schemas import redact_text  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.ticket_store import TicketStore  # noqa: E402  pylint: disable=wrong-import-position
from coded_tools.sentinel.ticket_binding import checked_result, owns_ticket, job_owns_ticket  # noqa: E402
from run import invoke_agent  # noqa: E402  pylint: disable=wrong-import-position

LOGGER = logging.getLogger("sentinel.web")
STATIC_DIR = Path(__file__).resolve().parent / "web"
PORT = int(os.environ.get("SENTINEL_UI_PORT", "8501"))
MAX_BODY_BYTES = 64 * 1024
SNAPSHOT_TTL_SECONDS = 12
SOURCE_GROUPS = {
    "Metrics": {"metric"},
    "Logs": {"log"},
    "Deployments": {"deployment", "deployment_change", "commit"},
    "Runbooks": {"runbook"},
}

_lock = threading.RLock()
_conversation: Dict[str, Any] = {"id": None, "messages": [], "chat_context": {}, "sly_data": {}, "ticket_number": None,
                                 "alert_context": None, "running": False, "updated": 0.0}
_snapshots: Dict[int, tuple] = {}
_perf: Dict[str, float] = {"memory_peak_kb": 0.0, "duration_ms": 0.0}


# ----------------------------------------------------------------------------------------------- conversation state

def _restore() -> None:
    """Bring back the ongoing incident after a restart or page reload."""
    restored = conversation_store.restore_latest(interrupt_running=True)
    if restored:
        with _lock:
            _conversation.update({key: restored.get(key) for key in
                                  ("messages", "chat_context", "sly_data", "ticket_number", "alert_context", "running")})
            _conversation["id"] = restored["id"]
            _conversation["updated"] = time.time()


def _stored_state() -> Dict[str, Any]:
    return {key: _conversation.get(key) for key in
            ("messages", "chat_context", "sly_data", "ticket_number", "alert_context", "running")}


def _refresh_from_store() -> None:
    """A run finished by its worker thread (or by another process) is picked up here."""
    if not _conversation.get("running") or not _conversation.get("id"):
        return
    latest = conversation_store.load(_conversation["id"])
    if latest and not latest.get("running"):
        _conversation.update({key: latest.get(key) for key in
                              ("messages", "chat_context", "sly_data", "ticket_number", "running")})


def reset_incident() -> None:
    with _lock:
        if _conversation.get("running"):
            raise RuntimeError("An investigation is already running.")
        conversation_store.archive(_conversation.get("id"))
        _conversation.update({"id": None, "messages": [], "chat_context": {}, "sly_data": {}, "ticket_number": None,
                              "alert_context": None, "running": False, "updated": time.time()})


def start_turn(text: str, alert_context: Dict[str, Any], fresh: bool) -> None:
    """Runs one agent turn on a worker thread; the page polls /api/state for progress and the result."""
    with _lock:
        if _conversation.get("running"):
            raise RuntimeError("An investigation is already running.")
        if fresh:
            reset_incident()
        if alert_context:
            _conversation["alert_context"] = alert_context
        if not _conversation["id"]:
            _conversation["id"] = conversation_store.new_id()
        _conversation["messages"] = [*_conversation["messages"], {"role": "user", "content": text}]
        _conversation["running"] = True
        _conversation["updated"] = time.time()
        conversation_id = _conversation["id"]
        state = _stored_state()
        sly_data = dict(_conversation["sly_data"])
        sly_data.setdefault("incident_session_id", str(uuid.uuid4()))
        _conversation["sly_data"] = sly_data
        state = _stored_state()
        chat_context = dict(_conversation["chat_context"])
        conversation_store.persist(conversation_id, state)

    def worker() -> None:
        # Real, small memory profiling of one investigation run, surfaced in the UI's perf strip.
        started_tracing = not tracemalloc.is_tracing()
        if started_tracing:
            tracemalloc.start()
        turn_started = time.monotonic()
        try:
            response = invoke_agent(text, sly_data=sly_data, chat_context=chat_context)
            response["sly_data"] = checked_result(sly_data, response.get("sly_data") or {}, TicketStore())
            reply = {"role": "assistant", "content": response.get("text", "(no response)")}
            result = {"chat_context": response.get("chat_context"), "sly_data": response.get("sly_data")}
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.exception("Investigation failed")
            reply, result = {"role": "assistant", "content": redact_text(f"Error: {exc}")}, {}
        finally:
            duration_ms = (time.monotonic() - turn_started) * 1000
            _, peak_bytes = tracemalloc.get_traced_memory()
            if started_tracing:
                tracemalloc.stop()
            with _lock:
                _perf["memory_peak_kb"] = round(peak_bytes / 1024, 1)
                _perf["duration_ms"] = round(duration_ms, 1)
        with _lock:
            if _conversation.get("id") == conversation_id:
                _conversation["messages"] = [*_conversation["messages"], reply]
                _conversation["chat_context"] = result.get("chat_context") or _conversation["chat_context"]
                _conversation["sly_data"] = result.get("sly_data") or _conversation["sly_data"]
                _conversation["ticket_number"] = _conversation["sly_data"].get("sentinel_ticket_number") or _conversation["ticket_number"]
                _conversation["running"] = False
                _conversation["updated"] = time.time()
                final = _stored_state()
            else:
                final = {**state, "messages": [*state["messages"], reply], "running": False}
            # Finish persistence under the same lock as admission of the next turn.
            conversation_store.persist(conversation_id, final)

    def bind_ticket() -> None:
        """Bind only this investigation's ticket, even when a webhook opens another concurrently."""
        for _ in range(600):
            time.sleep(1.0)
            with _lock:
                if not _conversation.get("running") or _conversation.get("id") != conversation_id:
                    return
                tickets = _load_tickets()
                ticket = next((t for t in tickets if t.get("session_id") == sly_data["incident_session_id"]), None)
                if ticket:
                    _conversation["ticket_number"] = ticket.get("number")
                    conversation_store.persist(conversation_id, _stored_state())
                    return

    threading.Thread(target=worker, daemon=True, name="sentinel-turn").start()
    threading.Thread(target=bind_ticket, daemon=True, name="sentinel-ticket-bind").start()


# -------------------------------------------------------------------------------------------------------- view models

def _load_tickets() -> List[Dict[str, Any]]:
    try:
        return TicketStore().load()
    except ValueError:
        return []


def _safe_url(value: Any) -> Optional[str]:
    """Only ordinary web links are ever sent to the page."""
    if not isinstance(value, str):
        return None
    parsed = urlparse(value)
    return value if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username else None


def _evidence_view(facts: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    evidence = facts.get("evidence") or []
    if not facts and not evidence:
        return None
    correlation = facts.get("correlation") or {}
    verdict = correlation.get("verdict")
    view: Dict[str, Any] = {"verdict": None, "groups": []}
    if verdict:
        view["verdict"] = {
            "label": "INSUFFICIENT EVIDENCE" if correlation.get("insufficient_evidence") else verdict,
            "supported": verdict == "SUPPORTED",
            "insufficient": bool(correlation.get("insufficient_evidence")),
            "reasoning": correlation.get("reasoning") or "No deterministic explanation was recorded.",
            "incident_time": correlation.get("incident_time"),
            "deploy_time": correlation.get("deploy_time"),
            "delta_minutes": correlation.get("delta_minutes"),
        }
    lookup, runbook, memory = facts.get("deployment_lookup") or {}, facts.get("runbook") or {}, facts.get("memory") or {}
    for name, types in SOURCE_GROUPS.items():
        records = [r for r in evidence if r.get("evidence_type") in types]
        group: Dict[str, Any] = {"name": name, "status": None, "records": [], "note": None}
        if records:
            group["status"] = " / ".join(sorted({str(r.get("source_status", "unknown")).upper() for r in records}))
            for record in records:
                fields = record.get("safe_fields") or {}
                group["records"].append({
                    "summary": record.get("summary") or record.get("evidence_type", "Evidence record"),
                    "source": record.get("source"), "id": record.get("record_id"),
                    "event_time": record.get("event_time"), "retrieved_at": record.get("retrieved_at"),
                    "window": [fields.get("window_start"), fields.get("window_end")] if fields.get("window_start") else None,
                    "url": _safe_url(record.get("source_url")),
                })
        elif name == "Deployments" and lookup.get("source_status"):
            group["status"] = str(lookup["source_status"]).upper()
            group["note"] = "No deployment evidence was published for this investigation."
        elif name == "Runbooks" and runbook:
            group["status"] = "MATCHED"
            group["records"].append({"summary": f"{runbook.get('id', 'Runbook')}: {runbook.get('title', '')}",
                                     "url": _safe_url(runbook.get("webui_link"))})
        else:
            group["status"] = "NO EVIDENCE"
            group["note"] = "No verified records were published for this source."
        view["groups"].append(group)
    if memory:
        view["memory"] = {"prior": memory.get("prior_incident_count", 0), "hotfix": memory.get("suggested_hotfix")}
    remediation = facts.get("remediation")
    if remediation:
        view["fix"] = {
            "summary": remediation.get("fix_summary"), "path": remediation.get("config_file"),
            "all_passed": bool(remediation.get("all_passed")),
            "checks": [{"name": c.get("name"), "status": c.get("status"), "detail": c.get("detail")}
                       for c in remediation.get("checks") or []],
        }
    return view


def _ticket_view(ticket: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not ticket:
        return None
    return {
        "number": ticket.get("number"), "state": ticket.get("state"), "service": ticket.get("service_name"),
        "priority": ticket.get("priority"), "caller": ticket.get("caller_id"), "group": ticket.get("assignment_group"),
        "assignee": ticket.get("assigned_to"), "servicenow_url": _safe_url(ticket.get("servicenow_url")),
        "pr_number": ticket.get("repair_pr_number"), "notes": ticket.get("work_notes") or [],
        "short_description": ticket.get("short_description"),
    }


def _snapshot(number: int) -> tuple:
    """One cached GitHub snapshot so frequent polling does not exhaust API limits."""
    cached = _snapshots.get(number)
    if cached and time.time() - cached[0] < SNAPSHOT_TTL_SECONDS:
        return cached[1]
    pr = github_pr_client.get_pull_request(number)
    checks = github_pr_client.get_check_runs(pr["head"]["sha"])
    diff = github_pr_client.get_pull_request_diff(number)
    files = github_pr_client.get_pull_request_files(number)
    issue_number = github_pr_client.linked_issue_number(pr)
    issue = github_pr_client.get_issue(issue_number) if issue_number else None
    value = (pr, checks, diff, issue, files)
    _snapshots[number] = (time.time(), value)
    return value


def _repair_eligible(ticket: Dict[str, Any]) -> bool:
    context = ticket.get("repair_context") or {}
    return bool(
        context.get("eligible") and context.get("ticket_number") == ticket.get("number")
        and context.get("session_id") == ticket.get("session_id")
        and context.get("service_name") == ticket.get("service_name")
        and context.get("verdict") == "SUPPORTED" and context.get("all_passed")
    )


def _repair_view(ticket: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Everything the review panel needs: what is proposed, the live PR with its diff and checks, and what is allowed."""
    view: Dict[str, Any] = {"state": "none", "message": "Start an incident to see the proposed fix here."}
    if not ticket:
        return view
    if ticket.get("service_name") != "checkout-service":
        return {"state": "none", "message": "The repair workflow is scoped to the checkout demo service."}
    context = ticket.get("repair_context") or {}
    if not _repair_eligible(ticket):
        return {"state": "locked", "message": "Locked until a SUPPORTED correlation and a passing validated fix exist for this ticket."}
    if not github_pr_client.is_configured():
        return {"state": "unconfigured", "message": "Connect the demo GitHub repository in .env to enable PR review."}
    patch = context.get("patch") or {}
    view.update({
        "state": "ready", "repository": github_pr_client.repository_name(),
        "change": {"summary": patch.get("summary"), "path": patch.get("path")} if patch else
                  {"summary": "Disable the injected checkout fault", "path": "app/settings.json"},
    })
    number = ticket.get("repair_pr_number")
    if not number:
        view["message"] = "A fix passed validation. Prepare the pull request to review it."
        return view
    try:
        pr, checks, diff, issue, files = _snapshot(int(number))
    except (github_pr_client.GithubPullRequestError, KeyError, ValueError, requests.RequestException) as exc:
        return {**view, "state": "error", "message": f"Could not load the pull request: {exc}"}
    scope_error = ""
    try:
        github_pr_client.verify_repair_pr(pr, files, ticket["number"], context, ticket.get("session_id", ""),
                                          require_open=not pr.get("merged"))
    except github_pr_client.GithubPullRequestError as exc:
        scope_error = str(exc)
    issue_number = github_pr_client.linked_issue_number(pr)
    passed = github_pr_client.checks_passed(checks)
    view.update({
        "state": "resolved" if pr.get("merged") and ticket.get("state") == "Resolved" else "merged" if pr.get("merged") else "review",
        "pr": {"number": pr["number"], "title": pr["title"], "url": _safe_url(pr["html_url"]), "state": pr["state"],
               "merged": bool(pr.get("merged")), "head": pr["head"]["ref"], "base": pr["base"]["ref"], "sha": pr["head"]["sha"]},
        "issue": {"number": issue_number, "url": _safe_url((issue or {}).get("html_url")),
                  "state": (issue or {}).get("state", "unknown")} if issue_number else None,
        "checks": [{"name": c.get("name", "Check"), "conclusion": c.get("conclusion") or c.get("status", "unknown"),
                    "url": _safe_url(c.get("details_url"))} for c in checks],
        "checks_passed": passed, "diff": diff or "", "scope_error": scope_error,
        "can_merge": bool(passed and pr.get("state") == "open" and not scope_error and not pr.get("merged")),
    })
    return view


def _job_view(job: Dict[str, Any]) -> Dict[str, Any]:
    return {key: job.get(key) for key in ("id", "alert_name", "service", "severity", "state", "received_at", "updated_at",
                                          "ticket_number", "error", "dependency_chain")}


def _webhook_health() -> Dict[str, Any]:
    status_url = os.environ.get("SENTINEL_WEBHOOK_STATUS_URL", "").strip()
    if status_url:
        try:
            requests.get(status_url, timeout=2).raise_for_status()
            return {"status": "listening", "where": "sentinel-webhook:8090"}
        except requests.RequestException:
            return {"status": "unavailable"}
    health = receiver_status()
    return {"status": health["status"], "where": f"{health.get('host')}:{health.get('port')}"}


def build_state() -> Dict[str, Any]:
    with _lock:
        _refresh_from_store()
        jobs = snapshot_jobs()
        latest_job = jobs[0] if jobs else None
        conv = dict(_conversation)
    tickets = {t.get("number"): t for t in _load_tickets()}

    job_iso = (latest_job or {}).get("updated_at") or ""
    conv_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(conv["updated"])) if conv["updated"] else ""
    use_job = bool(latest_job) and not conv["running"] and (not conv["messages"] or job_iso > conv_iso)
    facts: Dict[str, Any]
    if use_job:
        job = latest_job or {}
        ticket = tickets.get(job.get("ticket_number"))
        if not job_owns_ticket(ticket, job):
            ticket = None
        facts = job.get("verified_facts") or {}
        messages: List[Dict[str, str]] = []
        if job.get("verified_markdown") or job.get("response"):
            messages = [{"role": "assistant", "content": "\n\n".join(
                part for part in (job.get("verified_markdown"), job.get("response")) if part)}]
        source = {"kind": "alertmanager", "title": job.get("alert_name"), "service": job.get("service"),
                  "severity": job.get("severity"), "time": job.get("received_at"), "state": job.get("state"),
                  "error": job.get("error")}
        running = job.get("state") in ("queued", "investigating")
        dependency_chain = job.get("dependency_chain") or []
    else:
        number = conv.get("ticket_number") or (conv.get("sly_data") or {}).get("sentinel_ticket_number")
        ticket = tickets.get(number)
        if not owns_ticket(ticket, conv.get("sly_data") or {}):
            ticket = None
        facts = (conv.get("sly_data") or {}).get("sentinel_verified") or {}
        messages = conv.get("messages") or []
        context = conv.get("alert_context") or {}
        source = {"kind": "manual" if messages else "idle", "title": context.get("title"), "service": context.get("service"),
                  "severity": context.get("severity"), "time": context.get("event_time"), "origin": context.get("origin")}
        running = bool(conv.get("running"))
        dependency_chain = []

    return {
        "now": time.time(), "running": running, "source": source, "ticket": _ticket_view(ticket),
        "evidence": _evidence_view(facts), "repair": _repair_view(ticket), "messages": messages,
        "dependency_chain": dependency_chain,
        "jobs": [_job_view(job) for job in jobs[:6]], "webhook": _webhook_health(),
        "integrations": {
            "mode": "demo" if demo_mode() else "live",
            "servicenow": servicenow_client.is_configured(), "github": github_pr_client.is_configured(),
            "demo_app_url": os.environ.get("SENTINEL_DEMO_APP_PUBLIC_URL") or os.environ.get("SENTINEL_DEMO_APP_URL", ""),
        },
        "perf": {**_perf, "confluence_cache": cache_stats()},
    }


# ------------------------------------------------------------------------------------------------------- repair flow

def _audit(ticket: Dict[str, Any], event: str, details: Dict[str, Any]) -> None:
    TicketStore().append_audit_event(
        event, "ticket", ticket["number"], fingerprint=ticket.get("alert_fingerprint"),
        session_id=ticket.get("session_id"), details={"actor": "web-user", **details})


def _record_pr_ticket_update(ticket: Dict[str, Any], note: str, resolve: bool = False, pr_number: Optional[int] = None) -> None:
    """Record the human-reviewed PR outcome locally and on the linked ServiceNow incident."""
    note = redact_text(note)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    changes: Dict[str, Any] = {"append_work_notes": [f"{now}: {note}"], "sys_updated_on": now}
    if pr_number is not None:
        changes["repair_pr_number"] = str(pr_number)
    if resolve:
        changes["state"] = "Resolved"
    store = TicketStore()
    sys_id = ticket.get("servicenow_sys_id")
    if sys_id and servicenow_client.is_configured():
        outcome = "success"
        try:
            servicenow_client.update_incident(sys_id, note, resolve=resolve, close_notes=note if resolve else "")
        except servicenow_client.ServiceNowError:
            outcome = "failure"
            raise
        finally:
            _audit(ticket, "connector.servicenow.recovery_update",
                   {"connector": "servicenow", "outcome": outcome, "to_state": "Resolved" if resolve else ticket.get("state")})
    store.update(ticket["number"], changes)


def _finalize_merged_pr(ticket: Dict[str, Any], pr_number: int, expected_sha: str) -> None:
    """Apply the reviewed change to the demo app, verify checkout, and resolve the linked ticket."""
    context = ticket.get("repair_context") or {}
    path = github_pr_client.sync_merged_settings(pr_number, ticket["number"], context, ticket.get("session_id", ""), expected_sha)
    app_url = os.environ.get("SENTINEL_DEMO_APP_URL", "http://127.0.0.1:8000").rstrip("/")
    requests.post(f"{app_url}/demo/recover", timeout=5).raise_for_status()
    probe = requests.post(f"{app_url}/checkout", json={"order_id": "sentinel-recovery-check", "country": "IN", "subtotal": 49.9}, timeout=5)
    probe.raise_for_status()
    _record_pr_ticket_update(
        ticket,
        f"[Human approval] GitHub PR #{pr_number} merged after all Actions checks passed. "
        f"Merged change applied to the demo app ({path}); checkout recovery verified with an order from IN.",
        resolve=True, pr_number=pr_number)


def _current_ticket(expected_number: str) -> Dict[str, Any]:
    if not isinstance(expected_number, str) or not expected_number:
        raise ValueError("A reviewed ticket number is required.")
    state = build_state()
    number = (state.get("ticket") or {}).get("number")
    ticket = TicketStore().get(number) if number else None
    if not ticket:
        raise ValueError("No incident ticket is active.")
    if number != expected_number:
        raise ValueError("The active ticket changed; refresh and review it again.")
    return ticket


def api_create_pr(expected_ticket: str = None) -> Dict[str, Any]:
    if not expected_ticket:
        raise ValueError("A reviewed ticket number is required.")
    ticket = _current_ticket(expected_ticket)
    if not _repair_eligible(ticket) or ticket.get("repair_pr_number"):
        raise ValueError("A repair PR cannot be created for this ticket.")
    try:
        pr = github_pr_client.create_repair_pr(ticket["number"], ticket.get("repair_context") or {}, ticket.get("session_id", ""))
    except github_pr_client.GithubPullRequestError:
        _audit(ticket, "connector.github.pr_create", {"connector": "github", "outcome": "failure"})
        raise
    store = TicketStore()
    store.update(ticket["number"], {"repair_pr_number": str(pr["number"]),
                                    "sys_updated_on": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    _audit(ticket, "repair.pr_created", {"outcome": "success", "repair_pr_number": str(pr["number"])})
    issue = f"GitHub issue #{pr['sentinel_issue_number']} created first: {pr['sentinel_issue_url']}. " if pr.get("sentinel_issue_number") else ""
    ticket_notes.add_note({"sentinel_ticket_number": ticket["number"], "incident_session_id": ticket.get("session_id")},
                          f"[GitHub] {issue}Repair PR #{pr['number']} opened for human review: {pr['html_url']}")
    _snapshots.clear()
    return {"pr": pr["number"]}


def api_merge(approved: bool, expected_ticket: str = None, expected_pr: int = None, expected_sha: str = None) -> Dict[str, Any]:
    """The human's explicit decision: only runs when the page sends the approval the reviewer ticked."""
    if not approved:
        raise ValueError("Merging requires explicit approval.")
    ticket = _current_ticket(expected_ticket)
    number = int(ticket.get("repair_pr_number") or 0)
    if not number:
        raise ValueError("There is no repair PR to merge.")
    _snapshots.clear()
    pr, checks, _diff, _issue, _files = _snapshot(number)
    if pr.get("merged") or pr.get("state") != "open" or not github_pr_client.checks_passed(checks):
        raise ValueError("The PR is not open with all checks passing.")
    sha = pr["head"]["sha"]
    if type(expected_pr) is not int or expected_pr != number or not expected_sha or expected_sha != sha:
        raise ValueError("The reviewed PR or commit changed; review it again.")
    context = ticket.get("repair_context") or {}
    _audit(ticket, "human.approval", {"decision": "approve_merge", "outcome": "requested", "repair_pr_number": str(number)})
    try:
        merged = github_pr_client.merge_pull_request(number, sha, ticket["number"], context, ticket.get("session_id", ""))
        if not merged.get("merged"):
            raise github_pr_client.GithubPullRequestError(merged.get("message", "GitHub did not merge the PR."))
    except Exception:
        _audit(ticket, "human.approval", {"decision": "approve_merge", "outcome": "failure", "repair_pr_number": str(number)})
        raise
    _audit(ticket, "human.approval", {"decision": "approve_merge", "outcome": "success", "repair_pr_number": str(number)})
    _snapshots.clear()
    return _finalize_ticket(ticket, number, sha)


def api_finalize(expected_ticket: str = None, expected_pr: int = None, expected_sha: str = None) -> Dict[str, Any]:
    ticket = _current_ticket(expected_ticket)
    return _finalize_ticket(ticket, expected_pr, expected_sha)


def _finalize_ticket(ticket: Dict[str, Any], expected_pr: int, expected_sha: str) -> Dict[str, Any]:
    number = int(ticket.get("repair_pr_number") or 0)
    if not number:
        raise ValueError("There is no merged repair PR.")
    _snapshots.clear()
    pr = _snapshot(number)[0]
    if not pr.get("merged"):
        raise ValueError("The PR has not been merged yet.")
    if type(expected_pr) is not int or expected_pr != number or not expected_sha or expected_sha != pr["head"]["sha"]:
        raise ValueError("The reviewed PR or commit changed; review it again.")
    _audit(ticket, "repair.finalize", {"decision": "verify_recovery", "outcome": "attempted", "repair_pr_number": str(number)})
    try:
        _finalize_merged_pr(ticket, number, pr["head"]["sha"])
    except Exception:
        _audit(ticket, "repair.finalize", {"decision": "verify_recovery", "outcome": "failure", "repair_pr_number": str(number)})
        raise
    _audit(ticket, "repair.finalize", {"decision": "verify_recovery", "outcome": "recovery_verified", "repair_pr_number": str(number)})
    _snapshots.clear()
    return {"resolved": True}


def api_investigate(body: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(body.get("text", ""), str) or not isinstance(body.get("fresh", False), bool):
        raise ValueError("text must be a string and fresh must be a boolean.")
    text = body.get("text", "").strip()
    if len(text) > 4000:
        raise ValueError("Incident text must be at most 4000 characters.")
    if text:
        start_turn(text, {"origin": "Free-text user input", "title": "User-described incident"}, fresh=body.get("fresh", False))
        return {"started": True}
    preset = str(body.get("preset") or "")
    if preset not in list_presets():
        raise ValueError("Unknown sample alert.")
    alert = load_preset(preset)
    labels = {}
    for tag in alert.get("tags") or []:
        if isinstance(tag, str) and ":" in tag:
            key, value = tag.split(":", 1)
            labels[key.strip().lower()] = value.strip()
    context = {
        "origin": f"Sample alert: {preset}", "title": alert.get("title") or alert.get("alert_type") or "Sample alert",
        "service": labels.get("service") or alert.get("service"), "environment": labels.get("env") or alert.get("environment"),
        "severity": alert.get("severity"), "event_time": alert.get("date") or alert.get("fired_at"),
    }
    start_turn(compose_incident_report(alert), context, fresh=True)
    return {"started": True}


# ------------------------------------------------------------------------------------------------------------ server

REQUEST_TIMEOUT_SECONDS = 10


class Handler(BaseHTTPRequestHandler):
    """Static files plus a small JSON API. Writes require a custom header, so other sites cannot trigger them."""

    server_version = "Sentinel"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)

    def _json(self, status: int, body: Any) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, name: str) -> None:
        path = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in path.parents or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # pylint: disable=invalid-name
        route = urlparse(self.path).path
        try:
            if route == "/healthz":
                self._json(200, {"status": "ok"})
            elif route == "/api/state":
                self._json(200, build_state())
            elif route == "/api/presets":
                self._json(200, {"presets": list_presets()})
            elif route in ("/", "/index.html"):
                self._static("index.html")
            else:
                self._static(route.lstrip("/"))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.exception("GET %s failed", route)
            self._json(500, {"error": redact_text(str(exc))})

    def do_POST(self) -> None:  # pylint: disable=invalid-name
        route = urlparse(self.path).path
        if self.headers.get("X-Sentinel") != "1":
            self._json(403, {"error": "missing X-Sentinel header"})
            return
        try:
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                raise ValueError("A single Content-Length header is required.")
            length = int(self.headers["Content-Length"])
            if length < 0:
                raise ValueError("Content-Length must not be negative.")
            if length > MAX_BODY_BYTES:
                self._json(413, {"error": "Request body is too large."})
                # A close with unread upload bytes can reset TCP before Windows
                # clients receive the 413. Send the response/FIN first, then
                # allow a bounded drain instead of accepting an unbounded body.
                self.wfile.flush()
                self.close_connection = True
                try:
                    self.connection.shutdown(socket.SHUT_WR)
                    self.connection.settimeout(1)
                    self.rfile.read(min(length, MAX_BODY_BYTES + 1))
                except OSError:
                    pass  # Client disconnect or drain deadline after rejection.
                return
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError("Incomplete request body.")
            body = json.loads(raw or b"{}")
            if not isinstance(body, dict):
                raise ValueError("Request body must be a JSON object.")
            if route == "/api/investigate":
                result = api_investigate(body)
            elif route == "/api/new":
                reset_incident()
                result = {"ok": True}
            elif route == "/api/pr/create":
                result = api_create_pr(body.get("ticket_number"))
            elif route == "/api/pr/merge":
                result = api_merge(body.get("approved") is True, body.get("ticket_number"), body.get("pr_number"), body.get("sha"))
            elif route == "/api/pr/finalize":
                result = api_finalize(body.get("ticket_number"), body.get("pr_number"), body.get("sha"))
            else:
                self._json(404, {"error": "not found"})
                return
            self._json(200, result)
        except TimeoutError:
            self._json(408, {"error": "Request body timed out."})
        except (ValueError, RuntimeError, github_pr_client.GithubPullRequestError, servicenow_client.ServiceNowError,
                requests.RequestException, OSError) as exc:
            self._json(400, {"error": redact_text(str(exc))})
        except Exception as exc:  # pylint: disable=broad-exception-caught
            LOGGER.exception("POST %s failed", route)
            self._json(500, {"error": redact_text(str(exc))})

    def log_message(self, format_string: str, *args: Any) -> None:  # noqa: D401
        LOGGER.debug(format_string, *args)


def main() -> None:
    if os.environ.get("SENTINEL_RECEIVER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}:
        start_receiver()
    _restore()
    server = ThreadingHTTPServer((os.environ.get("SENTINEL_UI_HOST", "127.0.0.1"), PORT), Handler)
    LOGGER.warning("Sentinel UI listening on :%s", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
