"""Offline privacy, provenance, and correlation regressions for tool evidence."""

from datetime import datetime, timezone
from pathlib import Path

from coded_tools.sentinel import prometheus_metrics
from coded_tools.sentinel.correlation_check import CorrelationCheck
from coded_tools.sentinel.loki_logs import LokiLogs
from coded_tools.sentinel.prometheus_metrics import PrometheusMetrics
from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.verified_facts import publish_evidence
from run import format_verified_facts


def test_evidence_record_filters_fields_and_scrubs_nested_pii() -> None:
    record = EvidenceRecord(
        evidence_type="metric",
        service_name="checkout-service",
        source="prometheus",
        source_status=SourceStatus.LIVE,
        retrieved_at=datetime(2026, 10, 12, 14, 3, tzinfo=timezone.utc),
        summary="Customer jane.doe@example.com saw errors",
        safe_fields={
            "metric": "checkout_requests_total",
            "labels": {"service": "checkout-service", "owner": "jane.doe@example.com"},
            "raw_response": "must not be retained",
        },
    )

    assert "jane.doe@example.com" not in record.summary
    assert "raw_response" not in record.safe_fields
    assert "jane.doe@example.com" not in record.safe_fields["labels"]["owner"]


def test_publish_evidence_deduplicates_and_preserves_provenance() -> None:
    evidence = {
        "evidence_type": "log",
        "service_name": "checkout-service",
        "source": "loki",
        "source_status": "live",
        "record_id": "log-1",
        "event_time": "2026-10-12T14:02:00Z",
        "retrieved_at": "2026-10-12T14:03:00Z",
        "summary": "HTTP 500",
    }
    sly_data: dict = {}
    publish_evidence(sly_data, [evidence, evidence])

    published = sly_data["sentinel_verified"]["evidence"]
    assert len(published) == 1
    assert published[0]["source_status"] == "live"
    assert published[0]["source"] == "loki"
    assert published[0]["event_time"] == "2026-10-12T14:02:00Z"


def test_prometheus_metrics_publishes_bounded_live_evidence(monkeypatch) -> None:
    class FakePrometheusClient:
        base_url = "http://127.0.0.1:9090"

        def query_range(self, _query, start, end, step):
            assert start == "2026-10-12T13:57:00Z"
            assert end == "2026-10-12T14:12:00Z"
            assert step == "15s"
            return {"result": [{"metric": {"service": "checkout-service", "status": "error"},
                                "values": [[1791813720, "0.25"]]}]}

    monkeypatch.setattr(prometheus_metrics, "PrometheusClient", FakePrometheusClient)
    sly_data: dict = {}
    result = PrometheusMetrics().invoke(
        {"service_name": "checkout-service", "starts_at": "2026-10-12T14:02:00Z"}, sly_data
    )

    assert result["source_status"] == "live"
    assert result["sample_count"] == 1
    item = sly_data["sentinel_verified"]["evidence"][0]
    assert item["source_status"] == "live"
    assert item["safe_fields"]["window_start"] == "2026-10-12T13:57:00+00:00"


def test_loki_normalization_redacts_email_and_card_data() -> None:
    rows = LokiLogs._normalize_streams(
        [{"stream": {"service": "checkout-service", "environment": "demo"},
          "values": [["1791813720000000000", 'ERROR customer=jane.doe@example.com card=4111-1111-1111-1111 " 500']]}],
        "checkout-service",
        "http://127.0.0.1:3100/loki/api/v1/query_range",
    )

    assert len(rows) == 1
    assert rows[0]["source_status"] == "live"
    assert "jane.doe@example.com" not in rows[0]["summary"]
    assert "4111-1111-1111-1111" not in rows[0]["summary"]
    assert rows[0]["safe_fields"]["labels"]["environment"] == "demo"


def test_correlation_marks_missing_matching_deployment_as_insufficient() -> None:
    sly_data = {
        "sentinel_verified": {
            "evidence": [{
                "evidence_type": "log",
                "service_name": "checkout-service",
                "source": "fixture:logs.json",
                "source_status": "fixture",
                "event_time": "2026-10-12T14:02:00Z",
                "safe_fields": {"environment": "prod"},
            }, {
                "evidence_type": "deployment_change",
                "service_name": "checkout-service",
                "source": "fixture:deploys.json",
                "source_status": "fixture",
                "event_time": "2026-10-12T13:58:00Z",
                "safe_fields": {"environment": "staging", "version": "v2.14.0"},
            }],
        },
    }

    result = CorrelationCheck().invoke(
        {"service_name": "checkout-service", "hypothesis": "The checkout-service deploy caused the incident."},
        sly_data,
    )

    assert result["verdict"] == "NOT_SUPPORTED"
    assert result["insufficient_evidence"] is True
    assert "insufficient data" in result["reasoning"]
    assert sly_data["sentinel_verified"]["correlation"]["insufficient_evidence"] is True
    markdown = format_verified_facts(sly_data)
    assert "INSUFFICIENT EVIDENCE" in markdown


def test_ticket_json_store_scrubs_sensitive_text_before_persisting(tmp_path: Path) -> None:
    store = TicketStore(data_file=tmp_path / "tickets.json")
    store.append({"number": "INC1200001", "state": "New", "work_notes": [
        "Contact jane.doe@example.com with card 4111-1111-1111-1111"
    ]})

    saved = (tmp_path / "tickets.json").read_text(encoding="utf-8")
    assert "jane.doe@example.com" not in saved
    assert "4111-1111-1111-1111" not in saved
