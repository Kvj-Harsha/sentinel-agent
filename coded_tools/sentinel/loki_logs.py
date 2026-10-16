"""Neuro SAN tool for bounded, redacted log evidence from Grafana Loki."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import hashlib
import logging
import os
import re
from typing import Any
from typing import Dict
from typing import Union
from urllib.parse import urlencode

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.logs_lookup import LogsLookup
from coded_tools.sentinel.mode import demo_mode
from coded_tools.sentinel.loki_client import LokiClient
from coded_tools.sentinel.loki_client import LokiError
from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import SourceStatus
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import redact_text
from coded_tools.sentinel.schemas import unavailable_result
from coded_tools.sentinel.ticket_notes import add_note
from coded_tools.sentinel.verified_facts import publish_evidence

logger = logging.getLogger(__name__)

MAX_LOGS = 20
MAX_LINE_CHARS = 500
SERVICE_SELECTORS = {"checkout-service": '{service="checkout-service"} |~ " 5[0-9]{2} "'}
STATUS_PATTERN = re.compile(r'"\s+(5\d{2})\b')
FIXTURE_FALLBACK_ENV = "SENTINEL_LOGS_FIXTURE_FALLBACK"


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("An exact ISO-8601 starts_at timestamp is required.")
    timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if timestamp.tzinfo is None:
        raise ValueError("starts_at must include a timezone.")
    return timestamp.astimezone(timezone.utc)


class LokiLogs(CodedTool):
    """Queries only the configured service label and a short alert-centered time range."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        service_name = str(args.get("service_name") or "").strip().lower()
        if demo_mode():
            return LogsLookup().invoke({"service_name": service_name}, sly_data)
        logql = SERVICE_SELECTORS.get(service_name)
        source = "loki"
        if not logql:
            return unavailable_result(source, f"No Loki query is configured for service '{service_name or 'unknown'}'.")
        try:
            incident_time = _parse_time(args.get("starts_at"))
        except (TypeError, ValueError, OverflowError):
            return unavailable_result(source, "Could not query logs: supply the alert's exact timezone-aware startsAt timestamp.")

        start = incident_time - timedelta(minutes=5)
        end = incident_time + timedelta(minutes=10)
        try:
            client = LokiClient()
            streams = client.query_range(logql, int(start.timestamp() * 1_000_000_000), int(end.timestamp() * 1_000_000_000), MAX_LOGS)
        except LokiError as exc:
            logger.info("Loki logs unavailable: %s", exc)
            if _fixture_fallback_enabled():
                fixture = LogsLookup().invoke(
                    {"service_name": service_name, "query": args.get("query", "")}, sly_data
                )
                if isinstance(fixture, dict):
                    fixture["summary"] = f"Loki unavailable; explicitly enabled fixture fallback used. {fixture.get('summary', '')}"
                    fixture["fixture_fallback_reason"] = str(exc)
                    return fixture
            return unavailable_result(source, f"Loki logs unavailable: {exc}")

        query_params = {
            "query": logql,
            "start": str(int(start.timestamp() * 1_000_000_000)),
            "end": str(int(end.timestamp() * 1_000_000_000)),
            "limit": str(MAX_LOGS),
            "direction": "forward",
        }
        source_url = f"{client.base_url}/loki/api/v1/query_range?{urlencode(query_params)}"
        try:
            records = self._normalize_streams(streams, service_name, source_url)
        except (TypeError, ValueError, OverflowError):
            return unavailable_result(source, "Loki returned malformed log entries.")

        if records:
            publish_evidence(sly_data, records)
            add_note(sly_data, f"[Logs] Loki returned {len(records)} sanitized HTTP 5xx log entries for {service_name}.",
                     once_key=f"loki-logs:{service_name}:{start.isoformat()}")

        summary = (
            f"Loki returned {len(records)} live HTTP 5xx log entries for {service_name}."
            if records else f"Loki was reachable but found no matching HTTP 5xx logs for {service_name} in this time window."
        )
        return {
            "matches": records,
            "evidence": records,
            "source": "loki",
            "source_status": SourceStatus.LIVE.value,
            "query": logql,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "source_url": source_url,
            "summary": summary,
        }

    @staticmethod
    def _normalize_streams(streams: list[dict[str, Any]], service_name: str, source_url: str) -> list[dict[str, Any]]:
        records = []
        retrieved_at = datetime.now(timezone.utc)
        for stream in streams:
            if not isinstance(stream, dict) or not isinstance(stream.get("stream"), dict) or not isinstance(stream.get("values"), list):
                raise ValueError("bad stream")
            labels = stream["stream"]
            safe_labels = {key: labels[key] for key in ("service", "job", "container", "environment") if key in labels and isinstance(labels[key], str)}
            for point in stream["values"]:
                if not isinstance(point, list) or len(point) != 2:
                    raise ValueError("bad log point")
                event_ns, raw_line = point
                if not isinstance(raw_line, str):
                    raise ValueError("bad log line")
                epoch_ns = int(event_ns)
                seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
                event_time = datetime.fromtimestamp(seconds, tz=timezone.utc) + timedelta(microseconds=nanoseconds // 1_000)
                exact_event_time = f"{event_time:%Y-%m-%dT%H:%M:%S}.{nanoseconds:09d}Z"
                sanitized = redact_text(raw_line)
                excerpt = sanitized[:MAX_LINE_CHARS]
                match = STATUS_PATTERN.search(raw_line)
                status = int(match.group(1)) if match else 500
                record_id = hashlib.sha256(f"{epoch_ns}:{excerpt}".encode("utf-8")).hexdigest()[:20]
                records.append(dump_model(EvidenceRecord(
                    evidence_type="log",
                    service_name=service_name,
                    source="loki",
                    source_status=SourceStatus.LIVE,
                    query=SERVICE_SELECTORS[service_name],
                    record_id=record_id,
                    event_time=event_time,
                    retrieved_at=retrieved_at,
                    summary=f"HTTP {status} response at {exact_event_time}: {excerpt}",
                    safe_fields={"severity": "ERROR", "status_code": status, "labels": safe_labels,
                                 "message_excerpt": excerpt, "timestamp_unix_ns": str(epoch_ns),
                                 "event_timestamp": exact_event_time},
                    source_url=source_url,
                )))
        records.sort(key=lambda item: item["event_time"])
        return records[:MAX_LOGS]

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Loki requests have a short bounded timeout; reuse the synchronous adapter."""
        return self.invoke(args, sly_data)


def _fixture_fallback_enabled() -> bool:
    return os.environ.get(FIXTURE_FALLBACK_ENV, "false").strip().lower() in {"1", "true", "yes"}
