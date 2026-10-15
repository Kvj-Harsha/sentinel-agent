"""Helper for publishing tool-verified facts through sly_data (the framework's private channel)."""

from typing import Any
from typing import Dict

VERIFIED_KEY = "sentinel_verified"


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
    sly_data.setdefault(VERIFIED_KEY, {})[section] = facts
