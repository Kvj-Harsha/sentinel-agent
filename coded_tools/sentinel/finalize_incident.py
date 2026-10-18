"""Coded tool that closes out an investigation: records incident memory and prepares the repair proposal."""

import logging
from typing import Any
from typing import Dict
from typing import Optional
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

import os
import time

from coded_tools.sentinel import github_pr_client
from coded_tools.sentinel import ticket_notes
from coded_tools.sentinel.mode import demo_mode
from coded_tools.sentinel.record_incident import RecordIncident
from coded_tools.sentinel.remediation_pipeline import RemediationPipeline
from coded_tools.sentinel.remediation_pipeline import plan_exists
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.ticket_binding import owns_ticket
from coded_tools.sentinel.update_ticket import UpdateTicket

logger = logging.getLogger(__name__)

_EVIDENCE_LABELS = (("log", "Logs"), ("metric", "Metrics"), ("deployment", "Deployments"),
                    ("deployment_change", "Deployments"), ("runbook", "Runbooks"))


def build_report(sly_data: Dict[str, Any]) -> str:
    """
    Renders the facts a human needs from tool-published data only, so the final answer cannot misstate the verdict,
    the runbook, or whether a source was fixture or live (small models paraphrase these wrongly).
    """
    facts = sly_data.get("sentinel_verified") or {}
    correlation = facts.get("correlation") or {}
    ticket = TicketStore().get(sly_data.get("sentinel_ticket_number") or "") or {}
    lines = [f"Ticket: {ticket.get('number', 'unknown')} ({ticket.get('state', 'unknown')})",
             f"Correlation verdict: {correlation.get('verdict', 'unknown')} - {correlation.get('reasoning', '')}".strip()]
    runbook = facts.get("runbook")
    runbook_status = next((r.get("source_status") for r in facts.get("evidence") or [] if r.get("evidence_type") == "runbook"), None)
    if runbook:
        link = f" ({runbook['webui_link']})" if runbook.get("webui_link") else ""
        lines.append(f"Runbook: {runbook['id']} - {runbook['title']}{link} [{runbook_status or 'unknown'}]")
    else:
        lines.append("Runbook: none matched")
    hotfix = (facts.get("memory") or {}).get("suggested_hotfix")
    lines.append(f"Suggested hotfix: {hotfix}" if hotfix else "Suggested hotfix: none on record")
    remediation = facts.get("remediation")
    if remediation:
        passed = sum(1 for check in remediation.get("checks", []) if check.get("status") == "passed")
        total = len(remediation.get("checks", []))
        lines.append(f"Proposed fix: {remediation.get('fix_summary')} ({remediation.get('config_file')}); sandbox checks {passed}/{total} passed")
    if ticket.get("repair_pr_number"):
        lines.append(f"Repair PR: #{ticket['repair_pr_number']} awaiting human review (not merged)")
    grouped: Dict[str, Dict[str, Any]] = {}
    for record in facts.get("evidence") or []:
        label = dict(_EVIDENCE_LABELS).get(record.get("evidence_type"))
        if label:
            entry = grouped.setdefault(label, {"count": 0, "status": set()})
            entry["count"] += 1
            entry["status"].add(record.get("source_status", "unknown"))
    for label in ("Logs", "Metrics", "Deployments", "Runbooks"):
        entry = grouped.get(label)
        lines.append(f"{label}: {entry['count']} record(s), {'/'.join(sorted(entry['status']))}" if entry else f"{label}: no evidence published")
    return "\n".join(lines)


def _open_repair_pr(number: str, sly_data: Dict[str, Any]) -> Optional[str]:
    """
    Live mode only: open the GitHub issue and repair PR automatically once the patch passed validation. A human still
    reviews the diff and CI and merges; this only removes the click that created the PR.
    """
    if demo_mode() or os.environ.get("SENTINEL_AUTO_PR", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return None
    store = TicketStore()
    ticket = store.get(number) or {}
    context = ticket.get("repair_context") or {}
    if ticket.get("repair_pr_number") or not context.get("eligible") or not github_pr_client.is_configured():
        return None
    try:
        pr = github_pr_client.create_repair_pr(number, context, ticket.get("session_id", ""))
    except github_pr_client.GithubPullRequestError as exc:
        ticket_notes.add_note(sly_data, f"[GitHub] Repair PR was not opened automatically: {exc}")
        return None
    store.update(number, {"repair_pr_number": str(pr["number"]), "sys_updated_on": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
    store.append_audit_event(
        "repair.pr_created", "ticket", number, fingerprint=ticket.get("alert_fingerprint"), session_id=ticket.get("session_id"),
        details={"actor": "sentinel-auto", "outcome": "success", "repair_pr_number": str(pr["number"])},
    )
    issue = f"GitHub issue #{pr['sentinel_issue_number']} created first: {pr['sentinel_issue_url']}. " if pr.get("sentinel_issue_number") else ""
    ticket_notes.add_note(sly_data, f"[GitHub] {issue}Repair PR #{pr['number']} opened for human review: {pr['html_url']}")
    return pr["html_url"]


class FinalizeIncident(CodedTool):
    """
    Deterministic close-out step for the Responder agent. Everything it needs (verdict, hypothesis, runbook,
    ticket number) is read from the verified facts the other tools published to sly_data, so the model only
    has to call it once. Each step is idempotent, so a repeated call changes nothing.

    Steps: (1) record the incident in persistent memory; (2) when the verdict is SUPPORTED, prepare the bounded
    config fix and run local preflight checks; (3) leave the ticket In Progress -- only the human-approved
    pull-request workflow in the UI may resolve it.
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: no arguments are required; ``summary`` may carry an optional one-sentence outcome.
        :param sly_data: reads sentinel_verified / sentinel_ticket_number and publishes remediation facts.
        :return: what was done, for the agent to report back.
        """
        facts = sly_data.get("sentinel_verified") or {}
        number = sly_data.get("sentinel_ticket_number")
        if number and not owns_ticket(TicketStore().get(number), sly_data):
            return "Error: Ticket does not belong to this incident session."
        correlation = facts.get("correlation")
        if not correlation:
            return "Error: no correlation_check verdict exists yet; call the Scribe before finalizing."

        service_name = correlation["service_name"]
        runbook_id = (facts.get("runbook") or {}).get("id")
        if correlation.get("delta_minutes") is not None:
            timing = f"deploy and incident {correlation['delta_minutes']} min apart"
        else:
            timing = "insufficient deploy/log evidence to compare timing"
        summary = f"{service_name}: {correlation['verdict']} -- {timing}" + (
            f"; matched runbook {runbook_id}." if runbook_id else "."
        )
        RecordIncident().invoke(
            {
                "service_name": service_name,
                "hypothesis": correlation["hypothesis"],
                "verdict": correlation["verdict"],
                "runbook_id": runbook_id,
                "summary": summary,
            },
            sly_data,
        )
        outcome = {"verdict": correlation["verdict"], "recorded": True, "repair_proposed": False}

        number = sly_data.get("sentinel_ticket_number")
        code_fix_ran = bool(facts.get("remediation"))
        # The offline replay keeps its fixture remediation plan; in live mode a repair exists only if the FixAgent's
        # patch passed validation, so a code bug is never "fixed" by an unrelated settings change.
        use_fixture_plan = demo_mode() and plan_exists(service_name)
        if correlation["verdict"] == "SUPPORTED" and number and (code_fix_ran or use_fixture_plan):
            if not facts.get("remediation"):
                RemediationPipeline().invoke({"service_name": service_name}, sly_data)
            remediation = (sly_data.get("sentinel_verified") or {}).get("remediation") or {}
            ticket = TicketStore().get(number) or {}
            if remediation.get("all_passed") and ticket.get("state") != "Resolved":
                UpdateTicket().invoke(
                    {
                        "number": number,
                        "work_note": (
                            f"Local preflight passed for the proposed fix from runbook {remediation.get('runbook_id')}. "
                            "GitHub PR checks, human approval, merge and recovery verification are still required."
                        ),
                    },
                    sly_data,
                )
            outcome["repair_proposed"] = bool(remediation)
            outcome["preflight_passed"] = bool(remediation.get("all_passed"))
        outcome["pr"] = _open_repair_pr(number, sly_data) if outcome["repair_proposed"] else None
        outcome["report"] = build_report(sly_data)
        outcome["summary"] = (
            "Incident recorded. "
            + ("Repair proposal prepared; a human must review and merge the PR." if outcome["repair_proposed"]
               else "No repair proposed (verdict not SUPPORTED or no allow-listed fix).")
        )
        return outcome

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method; all work is local and bounded."""
        return self.invoke(args, sly_data)
