"""Trusted incident facts (alert time, environment) taken from the alert text by code, never from an LLM's retelling."""

import re

from coded_tools.sentinel.schemas import redact_text
from typing import Any
from typing import Dict
from typing import Optional

CONTEXT_KEY = "sentinel_incident"
_ISO = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"
_FIRED_AT = re.compile(rf"alert fired at\s+({_ISO})", re.IGNORECASE)
_ANY_TIME = re.compile(_ISO)
_ENVIRONMENT = re.compile(r"Environment:\s*([A-Za-z0-9_-]+)", re.IGNORECASE)


def seed_incident_context(text: str, sly_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Record the alert's exact start time and environment in sly_data (the framework's private channel).
    Small models routinely invent or round timestamps when relaying them between agents, so evidence tools
    prefer these values over whatever an agent passes as an argument.
    """
    labelled = _FIRED_AT.search(text or "")
    anywhere = _ANY_TIME.search(text or "")
    environment = _ENVIRONMENT.search(text or "")
    context = {
        "starts_at": labelled.group(1) if labelled else (anywhere.group(0) if anywhere else None),
        "environment": environment.group(1).lower() if environment else None,
        # Redacted alert wording, so runbook search sees the real symptoms even if an agent paraphrases them away.
        "symptoms": redact_text((text or "")[:400]) if (labelled or anywhere) else None,
    }
    if context["starts_at"] or context["environment"]:
        sly_data[CONTEXT_KEY] = context
    return context


def trusted_value(sly_data: Dict[str, Any], key: str, llm_value: Optional[Any]) -> Optional[Any]:
    """Prefer the code-extracted value; fall back to the agent's argument when the alert had none."""
    return (sly_data.get(CONTEXT_KEY) or {}).get(key) or llm_value
