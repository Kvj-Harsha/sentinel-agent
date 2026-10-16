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
