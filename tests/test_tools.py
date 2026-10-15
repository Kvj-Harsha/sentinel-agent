"""
Fast, offline tests for Sentinel's coded tools and the alert composer (no LLM calls).

Run with:  pytest
"""

from pathlib import Path

import pytest
import requests
from alerts import compose_incident_report
from alerts import list_presets
from alerts import load_preset
from coded_tools.sentinel.correlation_check import CorrelationCheck
from coded_tools.sentinel.deploy_lookup import DeployLookup
from coded_tools.sentinel.github_deploy_lookup import GithubDeployLookup
from coded_tools.sentinel.incident_memory_store import IncidentMemoryStore
from coded_tools.sentinel.logs_lookup import LogsLookup
from coded_tools.sentinel.open_ticket import OpenTicket
from coded_tools.sentinel.recall_incidents import RecallIncidents
from coded_tools.sentinel.remediation_pipeline import RemediationPipeline
from coded_tools.sentinel.service_names import canonical_service_name
from coded_tools.sentinel import ticket_notes
from coded_tools.sentinel.record_incident import RecordIncident
from coded_tools.sentinel.runbook_search import RunbookSearch
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.update_ticket import UpdateTicket


def test_logs_lookup_returns_only_anomalies_by_default() -> None:
    """Baseline INFO entries are excluded unless asked for."""
    result = LogsLookup().invoke({"service_name": "checkout-service"}, {})
    assert result["matches"]
    assert all(m["level"] in ("ERROR", "WARN") for m in result["matches"])
    assert result["matches"][0]["timestamp"] == "2026-10-12T14:02:00Z"


def test_logs_lookup_vague_free_text_query_still_matches() -> None:
    """Regression: a multi-word paraphrase used to match nothing (whole-phrase substring match)."""
    result = LogsLookup().invoke({"query": "payment authorization failures"}, {})
    assert any(m["service"] == "payments-gateway" for m in result["matches"])


def test_deploy_lookup_most_recent_first() -> None:
    """Deploys are sorted newest first."""
    result = DeployLookup().invoke({"service_name": "checkout-service"}, {})
    assert result["matches"][0]["deployed_at"] == "2026-10-12T13:58:00Z"


def test_runbook_search_matches_and_carries_confluence_fields() -> None:
    """Best match is RB-042 and is shaped like a Confluence page."""
    result = RunbookSearch().invoke({"query": "checkout-service 500 NullPointerException PaymentValidator tax"}, {})
    top = result["matches"][0]
    assert top["id"] == "RB-042"
    assert top["space"]["key"] == "SRE"
    assert top["webui_link"].startswith("/wiki/spaces/SRE/")


def test_runbook_search_generic_words_do_not_false_match() -> None:
    """Regression: the word 'service' used to match every runbook title."""
    result = RunbookSearch().invoke({"query": "search-service 504 timeout calling search-index-cluster"}, {})
    assert not result["matches"]


@pytest.mark.parametrize(
    "service, hypothesis, expected",
    [
        ("checkout-service", "The checkout-service deploy introduced a tax-calc regression.", "SUPPORTED"),
        ("checkout-service", "Some unrelated regression caused 500s.", "NOT_SUPPORTED"),
        ("inventory-service", "The inventory-service deploy caused the pool exhaustion.", "NOT_SUPPORTED"),
        ("payments-gateway", "The payments-gateway deploy lowered a client timeout.", "SUPPORTED"),
        ("search-service", "The search-service client upgrade caused these timeouts.", "NOT_SUPPORTED"),
    ],
)
def test_correlation_check_verdicts(service: str, hypothesis: str, expected: str) -> None:
    """The deterministic evaluation loop re-derives timestamps itself and grades the hypothesis."""
    result = CorrelationCheck().invoke({"service_name": service, "hypothesis": hypothesis}, {})
    assert result["verdict"] == expected


def test_recall_incidents_suggests_hotfix_when_recurring(tmp_path: Path) -> None:
    """Two prior incidents with the same runbook -> an actionable suggested_hotfix, not just a count."""
    store = IncidentMemoryStore(data_file=tmp_path / "history.json")
    assert RecallIncidents(store=store).invoke({"service_name": "checkout-service"}, {})["suggested_hotfix"] is None

    sly_data: dict = {}
    for summary in ("First incident.", "Second incident."):
        sly_data = {}  # a new conversation per incident (same session id would be deduplicated)
        RecordIncident(store=store).invoke(
            {
                "service_name": "checkout-service",
                "hypothesis": "The checkout-service deploy caused these errors.",
                "verdict": "SUPPORTED",
                "runbook_id": "RB-042",
                "summary": summary,
            },
            sly_data,
        )
        assert "incident_session_id" in sly_data

    recalled = RecallIncidents(store=store).invoke({"service_name": "checkout-service"}, {})
    assert recalled["is_recurring"]
    assert recalled["suggested_hotfix"].startswith("Reapply the fix from RB-042")


def test_ticket_lifecycle_is_idempotent_per_conversation(tmp_path: Path) -> None:
    """A retried open_ticket reuses the ticket; update_ticket falls back to the conversation's ticket."""
    store = TicketStore(data_file=tmp_path / "tickets.json")
    sly_data: dict = {}
    args = {"service_name": "checkout-service", "short_description": "Elevated 500s"}

    opened = OpenTicket(store=store).invoke(args, sly_data)
    reopened = OpenTicket(store=store).invoke({**args, "short_description": "different"}, sly_data)
    assert opened["number"].startswith("INC")
    assert reopened["number"] == opened["number"]
    assert len(store.load()) == 1

    sly_data["sentinel_verified"] = {
        "correlation": {"verdict": "SUPPORTED"},
        "remediation": {"all_passed": True, "github_pr_merged": False, "recovery_verified": False},
    }
    pending = UpdateTicket(store=store).invoke({"work_note": "Preflight passed."}, sly_data)
    assert pending["number"] == opened["number"]
    assert pending["state"] == "In Progress"

    sly_data["sentinel_verified"]["remediation"]["github_pr_merged"] = True
    still_pending = UpdateTicket(store=store).invoke({"work_note": "PR merged; recovery pending."}, sly_data)
    assert still_pending["state"] == "In Progress"

    sly_data["sentinel_verified"]["remediation"]["recovery_verified"] = True
    resolved = UpdateTicket(store=store).invoke({"work_note": "Recovery verified."}, sly_data)
    assert resolved["number"] == opened["number"]
    assert resolved["state"] == "Resolved"

    missing = UpdateTicket(store=store).invoke({"number": "INC9999999", "work_note": "x"}, {})
    assert isinstance(missing, str) and missing.startswith("Error:")


def test_github_lookup_falls_back_to_offline_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any network failure -> the cached snapshot, so a live demo never breaks on connectivity."""

    def _offline(*_args: object, **_kwargs: object) -> None:
        raise requests.exceptions.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", _offline)
    result = GithubDeployLookup().invoke({"service_name": "sentinel-agent"}, {})
    assert result["matches"][0]["version"] == "cache-fallback"

    wrong = GithubDeployLookup().invoke({"service_name": "checkout-service"}, {})
    assert isinstance(wrong, str) and wrong.startswith("Error:")


def test_alert_presets_compose_a_report_naming_the_service() -> None:
    """Every sample alert becomes a report that names its affected service (no LLM involved)."""
    for name in list_presets():
        alert = load_preset(name)
        service = next(t.split(":", 1)[1] for t in alert["tags"] if t.startswith("service:"))
        assert service in compose_incident_report(alert)


def test_alert_without_service_tag_is_rejected() -> None:
    """An alert that doesn't identify its service can't be triaged."""
    with pytest.raises(ValueError):
        compose_incident_report({"title": "x", "body": "y", "tags": ["env:prod"]})


def test_record_incident_is_idempotent_per_incident_session(tmp_path: Path) -> None:
    """Regression: a live run recorded the same incident twice."""
    store = IncidentMemoryStore(data_file=tmp_path / "history.json")
    sly_data: dict = {}
    args = {
        "service_name": "checkout-service",
        "hypothesis": "The checkout-service deploy caused these errors.",
        "verdict": "SUPPORTED",
        "summary": "One incident.",
    }
    first = RecordIncident(store=store).invoke(args, sly_data)
    second = RecordIncident(store=store).invoke(args, sly_data)
    assert first["stored"] is True
    assert second["stored"] is False
    assert len(store.load()) == 1


def test_tools_publish_verified_facts_to_sly_data(tmp_path: Path) -> None:
    """The deterministic tools expose what they found, independent of the LLM's wording."""
    sly_data: dict = {}
    CorrelationCheck().invoke(
        {"service_name": "checkout-service", "hypothesis": "The checkout-service deploy caused this."}, sly_data
    )
    RunbookSearch().invoke({"query": "checkout-service 500 NullPointerException PaymentValidator tax"}, sly_data)
    store = IncidentMemoryStore(data_file=tmp_path / "history.json")
    RecallIncidents(store=store).invoke({"service_name": "checkout-service"}, sly_data)

    verified = sly_data["sentinel_verified"]
    assert verified["correlation"]["verdict"] == "SUPPORTED"
    assert verified["runbook"]["id"] == "RB-042"
    assert verified["memory"]["prior_incident_count"] == 0


@pytest.mark.parametrize("verdict", [None, "NOT_SUPPORTED"])
def test_ticket_is_never_resolved_without_a_supported_verdict(tmp_path: Path, verdict: object) -> None:
    """A model asking for 'Resolved' cannot close a ticket the deterministic check did not support."""
    store = TicketStore(data_file=tmp_path / "tickets.json")
    sly_data: dict = {}
    OpenTicket(store=store).invoke({"service_name": "inventory-service", "short_description": "Timeouts"}, sly_data)
    if verdict:
        sly_data["sentinel_verified"] = {"correlation": {"verdict": verdict}}

    result = UpdateTicket(store=store).invoke({"work_note": "Investigating.", "state": "Resolved"}, sly_data)
    assert result["state"] == "In Progress"


def test_servicenow_mirroring_creates_and_resolves_real_incident(tmp_path: Path, monkeypatch) -> None:
    """With credentials set, open/update call the Table API and the real INC number is used (mocked HTTP)."""
    from coded_tools.sentinel import servicenow_client  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("SERVICENOW_INSTANCE", "dev0")
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "x")
    calls = []

    def fake_request(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return [{"sys_id": "ref1", "user": {"value": "ref1", "display_value": "Beth Anglin"}}]
        return {"number": "INC0010099", "sys_id": "abc123"}

    monkeypatch.setattr(servicenow_client, "_request", fake_request)
    store = TicketStore(tmp_path / "tickets.json")
    sly_data = {}
    opened = OpenTicket(store).invoke({"service_name": "checkout-service", "short_description": "5xx"}, sly_data)
    assert opened["number"] == "INC0010099"
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][2]["state"] == "2" and posts[0][2]["assignment_group"] == "ref1"
    assert posts[0][2]["caller_id"] == "ref1" and posts[0][2]["assigned_to"] == "ref1"
    calls[:] = [c for c in calls if c[0] != "GET"]

    sly_data["sentinel_verified"] = {
        "correlation": {"verdict": "SUPPORTED"},
        "remediation": {"all_passed": True, "github_pr_merged": False, "recovery_verified": False},
    }
    UpdateTicket(store).invoke({"work_note": "preflight passed"}, sly_data)
    assert calls[-1][0] == "PATCH" and calls[-1][1] == "incident/abc123"
    assert "state" not in calls[-1][2]

    sly_data["sentinel_verified"]["remediation"]["github_pr_merged"] = True
    UpdateTicket(store).invoke({"work_note": "PR merged"}, sly_data)
    assert "state" not in calls[-1][2]

    sly_data["sentinel_verified"]["remediation"]["recovery_verified"] = True
    UpdateTicket(store).invoke({"work_note": "recovery verified"}, sly_data)
    assert calls[-1][2]["state"] == "6" and calls[-1][2]["close_code"]


def test_servicenow_failure_keeps_ticket_local(tmp_path: Path, monkeypatch) -> None:
    """An unreachable instance never breaks triage: the ticket is kept locally with a note."""
    from coded_tools.sentinel import servicenow_client  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("SERVICENOW_INSTANCE", "dev0")
    monkeypatch.setenv("SERVICENOW_USER", "admin")
    monkeypatch.setenv("SERVICENOW_PASSWORD", "x")

    def boom(*_args, **_kwargs):
        raise servicenow_client.ServiceNowError("down")

    monkeypatch.setattr(servicenow_client, "_request", boom)
    opened = OpenTicket(TicketStore(tmp_path / "t.json")).invoke(
        {"service_name": "checkout-service", "short_description": "5xx"}, {}
    )
    assert opened["number"].startswith("INC12")


def test_canonical_service_name_fixes_drift_but_not_unknown_services() -> None:
    """A slightly drifted name maps onto the real service; a genuinely unknown one stays unknown."""
    assert canonical_service_name("payment-gateway") == "payments-gateway"
    assert canonical_service_name("Checkout-Service ") == "checkout-service"
    assert canonical_service_name("banana-service") == "banana-service"
    assert LogsLookup().invoke({"service_name": "payment-gateway"}, {})["matches"]


def test_correlation_accepts_drifted_name_and_hypothesis() -> None:
    """The hypothesis may use the drifted spelling the model used throughout."""
    result = CorrelationCheck().invoke(
        {"service_name": "payment-gateway", "hypothesis": "The payment-gateway deploy caused the timeouts."}, {}
    )
    assert result["verdict"] == "SUPPORTED"


def _verified(verdict: str = "SUPPORTED") -> dict:
    return {"sentinel_verified": {"correlation": {"verdict": verdict}}}


def test_remediation_runs_checks_posts_notes_and_gates_resolution(tmp_path: Path, monkeypatch) -> None:
    """The fix is verified, every step lands on the ticket, and only then can the ticket resolve."""
    store = TicketStore(tmp_path / "t.json")
    monkeypatch.setattr(ticket_notes, "TicketStore", lambda: store)
    sly_data = _verified()
    OpenTicket(store).invoke({"service_name": "checkout-service", "short_description": "5xx"}, sly_data)

    pending = UpdateTicket(store).invoke({"work_note": "try", "state": "Resolved"}, sly_data)
    assert pending["state"] == "In Progress"  # fix not verified yet

    result = RemediationPipeline().invoke({"service_name": "checkout-service"}, sly_data)
    assert result["all_passed"] and len(result["checks"]) == 3
    assert {c["source"] for c in result["checks"]} == {"real"}
    notes = " ".join(store.load()[0]["work_notes"])
    assert "Secret scanning" in notes and "CodeQL" not in notes and "null_rate_guard: true" in notes

    again = RemediationPipeline().invoke({"service_name": "checkout-service"}, sly_data)
    assert "already ran" in again["summary"]
    assert UpdateTicket(store).invoke({"work_note": "preflight complete"}, sly_data)["state"] == "In Progress"

    sly_data["sentinel_verified"]["remediation"]["github_pr_merged"] = True
    assert UpdateTicket(store).invoke({"work_note": "PR merged"}, sly_data)["state"] == "In Progress"
    sly_data["sentinel_verified"]["remediation"]["recovery_verified"] = True
    assert UpdateTicket(store).invoke({"work_note": "recovery verified"}, sly_data)["state"] == "Resolved"


def test_remediation_refuses_without_verified_cause_and_blocks_on_failed_check(tmp_path: Path) -> None:
    """No SUPPORTED verdict -> no fix; a failing check (vulnerable dependency) blocks resolution."""
    refused = RemediationPipeline().invoke({"service_name": "checkout-service"}, _verified("NOT_SUPPORTED"))
    assert isinstance(refused, str) and refused.startswith("Error:")

    from coded_tools.sentinel.remediation_pipeline import load_plans  # pylint: disable=import-outside-toplevel
    import json  # pylint: disable=import-outside-toplevel

    plans = load_plans()
    plans["checkout-service"]["dependency_advisories"][0]["affected_versions"].append("3.0.0")
    plan_file = tmp_path / "plans.json"
    plan_file.write_text(json.dumps(plans), encoding="utf-8")
    result = RemediationPipeline(plan_file).invoke({"service_name": "checkout-service"}, _verified())
    assert not result["all_passed"]
    assert [c["name"] for c in result["checks"] if c["status"] == "failed"] == ["Dependency audit"]
