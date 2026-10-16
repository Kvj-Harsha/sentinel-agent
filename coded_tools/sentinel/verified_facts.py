"""Helper for publishing tool-verified facts through sly_data (the framework's private channel)."""

from typing import Any
from typing import Dict

from coded_tools.sentinel.schemas import EvidenceRecord
from coded_tools.sentinel.schemas import dump_model
from coded_tools.sentinel.schemas import sanitize_record

VERIFIED_KEY = "sentinel_verified"
# Cap evidence PER SOURCE TYPE. A single global cap let a chatty source (Loki can return ~20 log lines) evict the
# deployment and metric records that the correlation check needs.
MAX_RECORDS_PER_TYPE = 8


def publish(sly_data: Dict[str, Any], section: str, facts: Dict[str, Any]) -> None:
    """
    Records facts exactly as a coded tool computed them, under sly_data["sentinel_verified"][section].

    The front-man agent is allowed to return this key upstream (see the `allow` block in
    sentinel.hocon), so the UI can show what the deterministic tools actually found, independent of
    how the LLM later paraphrases it.

    :param sly_data: the conversation's sly_data dict (shared by all agents/tools in one request).
    :param section: which fact group this is ("runbook", "correlation", "memory").
    :param facts: JSON-serialisable facts straight from the tool's own result.
    """
    sly_data.setdefault(VERIFIED_KEY, {})[section] = sanitize_record(facts)


def publish_evidence(sly_data: Dict[str, Any], records: list[Dict[str, Any]]) -> None:
    """Validate and append compact evidence to the UI-visible verified facts channel."""
    verified = sly_data.setdefault(VERIFIED_KEY, {})
    existing = sanitize_record(verified.setdefault("evidence", []))
    verified["evidence"] = existing
    seen = {
        (item.get("source"), item.get("record_id"), item.get("event_time"), item.get("evidence_type"))
        for item in existing
    }
    for record in records:
        validated = dump_model(EvidenceRecord.model_validate(record))
        identity = (validated.get("source"), validated.get("record_id"), validated.get("event_time"), validated.get("evidence_type"))
        if identity not in seen:
            existing.append(validated)
            seen.add(identity)
    by_type: Dict[str, list] = {}
    for item in existing:
        by_type.setdefault(item.get("evidence_type", "record"), []).append(item)
    verified["evidence"] = [
        item for items in by_type.values() for item in _downsample(items, MAX_RECORDS_PER_TYPE)
    ]


def _downsample(items: list, limit: int) -> list:
    """Keep at most ``limit`` records, always including the first and last and spreading the rest evenly."""
    if len(items) <= limit:
        return items
    step = (len(items) - 1) / (limit - 1)
    return [items[round(index * step)] for index in range(limit)]
