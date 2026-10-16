"""Validated, provider-neutral contracts shared by Sentinel adapters and tools."""

from datetime import datetime
from enum import Enum
import re
from typing import Any
from typing import Dict
from typing import Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


class SourceStatus(str, Enum):
    """How a source produced the returned data."""

    LIVE = "live"
    FIXTURE = "fixture"
    CACHE = "cache"
    UNAVAILABLE = "unavailable"


SAFE_EVIDENCE_FIELDS = {
    "service", "service_name", "timestamp", "level", "status_code", "count", "trace_id", "pod", "message",
    "environment", "deployed_at", "version", "author", "change_summary", "labels", "severity",
    "message_excerpt", "timestamp_unix_ns", "event_timestamp", "metric", "value_per_second", "window_start",
    "window_end", "id", "title", "recommended_action", "related_incident", "match_score", "repository",
    "state", "commit_sha", "actor", "deployment_id", "deployment_created_at", "deployment_updated_at",
    "deployment_event_time", "deployment_url", "status_url",
}


class Incident(BaseModel):
    """Provider-neutral incident accepted at the alert adapter boundary."""

    model_config = ConfigDict(extra="forbid")

    incident_id: Optional[str] = None
    fingerprint: Optional[str] = None
    service_name: str = Field(min_length=1)
    environment: Optional[str] = None
    severity: Optional[str] = None
    title: str = Field(min_length=1)
    description: str = ""
    source: str = Field(min_length=1)
    fired_at: Optional[datetime] = None
    host: Optional[str] = None
    labels: Dict[str, str] = Field(default_factory=dict)
    source_url: Optional[str] = None
    source_payload_ref: Optional[str] = None

    @field_validator("incident_id", "fingerprint", "service_name", "environment", "severity", "title", "description", "source", "host", "source_url", "source_payload_ref", mode="before")
    @classmethod
    def redact_incident_text(cls, value: Any) -> Any:
        return redact_text(value) if isinstance(value, str) else value

    @field_validator("labels", mode="before")
    @classmethod
    def redact_incident_labels(cls, value: Any) -> Any:
        return sanitize_record(value)

    @field_validator("service_name", "title", "source", mode="before")
    @classmethod
    def trim_required_text(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class EvidenceRecord(BaseModel):
    """Compact evidence item with explicit provenance and allowlisted safe fields."""

    model_config = ConfigDict(extra="forbid")

    evidence_type: str = Field(min_length=1)
    service_name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_status: SourceStatus
    query: Optional[str] = None
    record_id: Optional[str] = None
    event_time: Optional[datetime] = None
    retrieved_at: datetime
    summary: str = ""
    safe_fields: Dict[str, Any] = Field(default_factory=dict)
    source_url: Optional[str] = None

    @field_validator("summary", mode="before")
    @classmethod
    def redact_summary(cls, value: Any) -> Any:
        return redact_text(value) if isinstance(value, str) else value

    @field_validator("query", "record_id", "source_url", mode="before")
    @classmethod
    def redact_metadata_text(cls, value: Any) -> Any:
        return redact_text(value) if isinstance(value, str) else value

    @field_validator("safe_fields", mode="before")
    @classmethod
    def redact_safe_fields(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return sanitize_record({key: item for key, item in value.items() if key in SAFE_EVIDENCE_FIELDS})


def dump_model(model: BaseModel) -> Dict[str, Any]:
    """Return a JSON-safe representation suitable for Neuro SAN tool results/sly_data."""
    return model.model_dump(mode="json", exclude_none=False)


def utc_now() -> datetime:
    """Timezone-aware retrieval timestamp."""
    from datetime import timezone

    return datetime.now(timezone.utc)


def unavailable_result(source: str, message: str) -> Dict[str, Any]:
    """Consistent structured failure; callers should not treat it as evidence of absence."""
    message = redact_text(message)
    return {"matches": [], "evidence": [], "source": source, "source_status": SourceStatus.UNAVAILABLE.value,
            "summary": message, "error": message}


def redact_text(value: str) -> str:
    """Mask common credentials, contact data, and payment data before persistence or model exposure."""
    value = re.sub(r"(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "[REDACTED_PRIVATE_KEY]", value)
    value = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    value = re.sub(r"\b(?:sk-(?:or-v1-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|ATATT[A-Za-z0-9_+/=-]{20,})\b", "[REDACTED_SECRET]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b", "[REDACTED_TOKEN]", value)
    value = re.sub(
        r"(?i)\b(password|passwd|pwd|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|authorization)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
        r"\1=[REDACTED]", value,
    )
    value = re.sub(
        r"(?i)\b(?:card(?:_?number)?|pan|cvv|cvc|security[_ ]?code)\b\s*[:=]\s*([^\s,;]+)",
        r"payment_data=[REDACTED]",
        value,
    )
    value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[REDACTED_EMAIL]", value)
    value = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_ID]", value)
    value = re.sub(
        r"(?<!\w)(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?|\d{3,5}[ .-])\d{3,5}[ .-]\d{3,5}(?!\w)",
        "[REDACTED_PHONE]", value,
    )
    value = re.sub(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)", _redact_luhn_card, value)
    return value


def _redact_luhn_card(match: re.Match[str]) -> str:
    """Mask 13-19 digit strings only when they pass the credit-card checksum."""
    digits = re.sub(r"\D", "", match.group(0))
    if not 13 <= len(digits) <= 19:
        return match.group(0)
    checksum = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        digit = int(char)
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return "[REDACTED_PAYMENT_DATA]" if checksum % 10 == 0 else match.group(0)


def sanitize_record(value: Any) -> Any:
    """Recursively redact strings in provider-shaped rows retained for legacy consumers."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [sanitize_record(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_record(item) for key, item in value.items()}
    return value
