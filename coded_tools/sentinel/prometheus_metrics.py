"""Neuro SAN tool for bounded, live Prometheus metric evidence."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import logging
from pathlib import Path
from typing import Any
from typing import Dict
from typing import Union
from urllib.parse import urlencode

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.incident_context import trusted_value
from coded_tools.sentinel.mode import demo_mode
from coded_tools.sentinel.prometheus_client import PrometheusClient
from coded_tools.sentinel.prometheus_client import PrometheusError
from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import unavailable_result
from coded_tools.sentinel.schemas import utc_now
from coded_tools.sentinel.verified_facts import publish_evidence

logger = logging.getLogger(__name__)

FIXTURE_FILE = Path(__file__).parent / "data" / "metrics.json"
MAX_LOOKBACK_MINUTES = 15
MAX_LOOKAHEAD_MINUTES = 15
MAX_SERIES = 25
MAX_SAMPLES = 500
STEP_SECONDS = 15
SERVICE_METRICS = {
    "checkout-service": 'sum by (service, status) (rate(checkout_requests_total{service="checkout-service",status="error"}[1m]))',
}


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An exact ISO-8601 starts_at timestamp is required.")
    timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("starts_at must include a timezone.")
    return timestamp.astimezone(timezone.utc)


class PrometheusMetrics(CodedTool):
    """Queries a small allowlist of service metrics around an exact alert timestamp."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        service_name = str(args.get("service_name") or "").strip().lower()
        if demo_mode():
            return self._replay_fixture(service_name, sly_data)
        promql = SERVICE_METRICS.get(service_name)
        source = "prometheus"
        if not promql:
            return unavailable_result(source, f"No Prometheus metric query is configured for service '{service_name or 'unknown'}'.")
        try:
            incident_time = _parse_time(trusted_value(sly_data, "starts_at", args.get("starts_at")))
        except (TypeError, ValueError, OverflowError):
            return unavailable_result(source, "Could not query metrics: supply the alert's exact timezone-aware startsAt timestamp.")

        start = incident_time - timedelta(minutes=5)
        end = incident_time + timedelta(minutes=10)
        try:
            client = PrometheusClient()
            data = client.query_range(
                promql,
                start=start.isoformat().replace("+00:00", "Z"),
                end=end.isoformat().replace("+00:00", "Z"),
                step=f"{STEP_SECONDS}s",
            )
        except PrometheusError as exc:
            logger.info("Prometheus metrics unavailable: %s", exc)
            return unavailable_result(source, f"Prometheus metrics unavailable: {exc}")

        results = data["result"]
        if len(results) > MAX_SERIES:
            return unavailable_result(source, f"Prometheus returned more than the {MAX_SERIES}-series limit.")
        raw_sample_count = sum(len(row.get("values", [])) for row in results if isinstance(row, dict))
        if raw_sample_count > MAX_SAMPLES:
            return unavailable_result(source, f"Prometheus returned more than the {MAX_SAMPLES}-sample limit.")

        retrieval_time = datetime.now(timezone.utc)
        evidence = []
        highest_rate = 0.0
        first_time = None
        last_time = None
        for row in results:
            if not isinstance(row, dict) or not isinstance(row.get("metric"), dict) or not isinstance(row.get("values"), list):
                return unavailable_result(source, "Prometheus returned malformed metric series.")
            labels = row["metric"]
            for point in row["values"]:
                try:
                    epoch, raw_value = point
                    event_time = datetime.fromtimestamp(float(epoch), tz=timezone.utc)
                    value = float(raw_value)
                except (TypeError, ValueError, OverflowError):
                    return unavailable_result(source, "Prometheus returned a malformed sample.")
                if not (value == value and abs(value) != float("inf")):
                    continue
                highest_rate = max(highest_rate, value)
                first_time = min(first_time, event_time) if first_time else event_time
                last_time = max(last_time, event_time) if last_time else event_time
                label_text = ",".join(f"{key}={value!r}" for key, value in sorted(labels.items()))
                record_id = f"checkout_requests_total:{label_text}:{event_time.isoformat()}"
                evidence.append(dump_model(EvidenceRecord(
                    evidence_type="metric",
                    service_name=service_name,
                    source=client.base_url,
                    source_status=SourceStatus.LIVE,
                    query=promql,
                    record_id=record_id,
                    event_time=event_time,
                    retrieved_at=retrieval_time,
                    summary=f"Checkout error rate was {value:.4f} errors/second at {event_time.isoformat()}.",
                    safe_fields={"metric": "checkout_requests_total", "labels": labels, "value_per_second": value,
                                 "window_start": start.isoformat(), "window_end": end.isoformat()},
                    source_url=f"{client.base_url}/graph?{urlencode({'g0.expr': promql})}",
                )))

        if evidence:
            publish_evidence(sly_data, evidence)
        if not results or not evidence:
            return {
                "matches": [], "evidence": [], "source": source,
                "source_status": SourceStatus.LIVE.value,
                "query": promql, "window_start": start.isoformat(), "window_end": end.isoformat(),
                "summary": "Prometheus was reachable, but it returned no usable samples in the bounded alert window.",
            }
        return {
            "matches": evidence, "evidence": evidence, "source": source,
            "source_status": SourceStatus.LIVE.value, "query": promql,
            "window_start": start.isoformat(), "window_end": end.isoformat(),
            "sample_count": len(evidence), "first_sample_at": first_time.isoformat() if first_time else None,
            "last_sample_at": last_time.isoformat() if last_time else None,
            "max_errors_per_second": highest_rate,
            "summary": f"Prometheus returned {len(evidence)} live checkout error-rate samples; peak {highest_rate:.4f} errors/second.",
        }

    @staticmethod
    def _replay_fixture(service_name: str, sly_data: Dict[str, Any]) -> Dict[str, Any]:
        """Replay bundled error-rate samples for the demo scenario; every record is labelled `fixture`."""
        source = "fixture:metrics.json"
        rows = [row for row in json.loads(FIXTURE_FILE.read_text(encoding="utf-8")) if row["service"] == service_name]
        if not rows:
            return unavailable_result(source, f"No replay metrics exist for service '{service_name or 'unknown'}'.")
        retrieved_at = utc_now()
        evidence = [dump_model(EvidenceRecord(
            evidence_type="metric", service_name=service_name, source=source, source_status=SourceStatus.FIXTURE,
            query="replay: checkout error rate", record_id=f"metrics.json:{row['timestamp']}",
            event_time=row["timestamp"], retrieved_at=retrieved_at,
            summary=f"Checkout error rate was {row['errors_per_second']:.2f} errors/second at {row['timestamp']}.",
            safe_fields={"metric": "checkout_requests_total", "value_per_second": row["errors_per_second"]},
        )) for row in rows]
        publish_evidence(sly_data, evidence)
        peak = max(row["errors_per_second"] for row in rows)
        return {"matches": evidence, "evidence": evidence, "source": source, "source_status": SourceStatus.FIXTURE.value,
                "sample_count": len(evidence), "max_errors_per_second": peak,
                "summary": f"Replay scenario: {len(evidence)} error-rate samples; peak {peak:.2f} errors/second."}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Prometheus HTTP requests use a short bounded timeout; reuse the sync adapter."""
        return self.invoke(args, sly_data)
