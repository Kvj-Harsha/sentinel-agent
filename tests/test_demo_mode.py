"""The default replay scenario must reach a SUPPORTED verdict with no network, using only labelled fixtures."""

import pytest

from coded_tools.sentinel.correlation_check import CorrelationCheck
from coded_tools.sentinel.github_deployments import GithubDeployments
from coded_tools.sentinel.loki_logs import LokiLogs
from coded_tools.sentinel.prometheus_metrics import PrometheusMetrics
from coded_tools.sentinel.runbook_search import RunbookSearch

STARTS_AT = "2026-10-12T14:02:00Z"
HYPOTHESIS = "The checkout-service v2.14.0 deploy introduced a NullPointerException in PaymentValidator.charge()."


@pytest.fixture(autouse=True)
def replay_mode(monkeypatch):
    monkeypatch.setenv("SENTINEL_MODE", "demo")
    monkeypatch.setenv("LOKI_URL", "http://127.0.0.1:1")  # any accidental live call would fail loudly


def test_replay_scenario_reaches_supported_verdict():
    sly_data: dict = {}
    args = {"service_name": "checkout-service", "starts_at": STARTS_AT}
    for tool, tool_args in (
        (LokiLogs(), args),
        (PrometheusMetrics(), args),
        (GithubDeployments(), {"service_name": "checkout-service", "environment": "prod"}),
    ):
        result = tool.invoke(tool_args, sly_data)
        assert result["source_status"] == "fixture"

    verdict = CorrelationCheck().invoke({"service_name": "checkout-service", "hypothesis": HYPOTHESIS}, sly_data)

    assert verdict["verdict"] == "SUPPORTED"
    assert verdict["delta_minutes"] == 4.0
    kinds = {record["evidence_type"] for record in sly_data["sentinel_verified"]["evidence"]}
    assert {"log", "metric", "deployment_change"} <= kinds
    assert all(record["source_status"] == "fixture" for record in sly_data["sentinel_verified"]["evidence"])


def test_replay_runbook_is_fixture_even_when_confluence_is_configured(monkeypatch):
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://example.invalid/wiki")
    result = RunbookSearch().invoke({"query": "checkout-service NullPointerException tax library deploy"}, {})
    assert result["source_status"] == "fixture"
    assert result["matches"]


def test_live_deployment_matches_when_logs_have_no_environment_label():
    """Loki lines have no environment label; the deployment lookup's environment must stand in for it."""
    sly_data = {"sentinel_verified": {
        "deployment_lookup": {"service_name": "checkout-service", "environment": "demo",
                              "source": "github:deployments", "source_status": "live", "record_count": 1},
        "evidence": [
            {"evidence_type": "log", "service_name": "checkout-service", "event_time": "2026-10-16T14:47:20Z",
             "source": "loki", "source_status": "live", "safe_fields": {"labels": {"service": "checkout-service"}}},
            {"evidence_type": "deployment", "service_name": "checkout-service", "event_time": "2026-10-16T14:45:27Z",
             "source": "github:deployments", "source_status": "live",
             "safe_fields": {"state": "success", "environment": "demo", "commit_sha": "715d49f"}},
        ]}}
    verdict = CorrelationCheck().invoke({"service_name": "checkout-service", "hypothesis": HYPOTHESIS}, sly_data)
    assert verdict["verdict"] == "SUPPORTED"


def test_chatty_source_cannot_evict_deployment_evidence():
    """20 log lines must not push the deployment record out of the verified evidence."""
    from coded_tools.sentinel.verified_facts import publish_evidence  # pylint: disable=import-outside-toplevel

    def record(kind: str, index: int) -> dict:
        return {"evidence_type": kind, "service_name": "checkout-service", "source": kind, "source_status": "live",
                "record_id": f"{kind}-{index}", "event_time": f"2026-10-16T14:47:{index:02d}Z", "summary": kind,
                "retrieved_at": "2026-10-16T14:50:00Z"}

    sly_data: dict = {}
    publish_evidence(sly_data, [record("deployment", 0)])
    publish_evidence(sly_data, [record("log", i) for i in range(20)])
    publish_evidence(sly_data, [record("metric", i) for i in range(48)])

    kept = sly_data["sentinel_verified"]["evidence"]
    assert [r["record_id"] for r in kept if r["evidence_type"] == "deployment"] == ["deployment-0"]
    logs = [r["record_id"] for r in kept if r["evidence_type"] == "log"]
    assert logs[0] == "log-0" and len(logs) == 8  # earliest log is always kept
    assert len([r for r in kept if r["evidence_type"] == "metric"]) == 8


def test_final_report_is_built_from_verified_facts(monkeypatch, tmp_path):
    from coded_tools.sentinel.finalize_incident import build_report  # pylint: disable=import-outside-toplevel

    sly_data: dict = {}
    args = {"service_name": "checkout-service", "starts_at": STARTS_AT}
    LokiLogs().invoke(args, sly_data)
    GithubDeployments().invoke({"service_name": "checkout-service"}, sly_data)
    RunbookSearch().invoke({"query": "checkout-service NullPointerException tax library deploy"}, sly_data)
    CorrelationCheck().invoke({"service_name": "checkout-service", "hypothesis": HYPOTHESIS}, sly_data)

    report = build_report(sly_data)

    assert "Correlation verdict: SUPPORTED" in report
    assert "Runbook: RB-042" in report and "[fixture]" in report
    assert "Deployments: 2 record(s), fixture" in report
    assert "Metrics: no evidence published" in report  # not collected in this test: reported honestly, not invented


def test_webhook_jobs_are_shared_through_sqlite(tmp_path):
    """The UI and the webhook can be separate containers: completed jobs must be readable from the database."""
    from coded_tools.sentinel.sqlite_store import SQLiteDatabase  # pylint: disable=import-outside-toplevel

    database = SQLiteDatabase(tmp_path / "jobs.db", seed_legacy_data=False)
    database.save_alert_job({"id": "job1", "state": "investigating", "updated_at": "2026-10-16T15:00:00+00:00"})
    database.save_alert_job({"id": "job1", "state": "completed", "updated_at": "2026-10-16T15:01:00+00:00",
                             "ticket_number": "INC1", "service": "checkout-service"})

    jobs = database.recent_alert_jobs()

    assert [(job["id"], job["state"], job["ticket_number"]) for job in jobs] == [("job1", "completed", "INC1")]


def test_alert_time_comes_from_the_alert_not_from_the_model(monkeypatch):
    """A model that invents starts_at must not redirect the evidence query."""
    from coded_tools.sentinel.incident_context import seed_incident_context  # pylint: disable=import-outside-toplevel

    monkeypatch.setenv("SENTINEL_MODE", "live")
    sly_data: dict = {}
    seed_incident_context(
        "[Triggered] High 5xx. Affected service: checkout-service. Environment: demo. (host: x, alert fired at 2026-10-16T14:47:09.502Z)",
        sly_data,
    )
    assert sly_data["sentinel_incident"] == {"starts_at": "2026-10-16T14:47:09.502Z", "environment": "demo"}

    seen = {}

    class FakeClient:
        base_url = "http://loki.invalid"

        def query_range(self, _query, start_ns, end_ns, _limit):
            seen["start"], seen["end"] = start_ns, end_ns
            return []

    monkeypatch.setattr("coded_tools.sentinel.loki_logs.LokiClient", FakeClient)
    LokiLogs().invoke({"service_name": "checkout-service", "starts_at": "2023-11-01T12:00:00Z"}, sly_data)

    from datetime import datetime, timezone  # pylint: disable=import-outside-toplevel
    expected = datetime(2026, 10, 16, 14, 42, 9, 502000, tzinfo=timezone.utc)
    assert seen["start"] == int(expected.timestamp() * 1_000_000_000)
