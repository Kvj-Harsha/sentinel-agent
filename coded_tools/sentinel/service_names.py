"""Deterministic resolution of the service names an LLM passes to the coded tools."""

import difflib
import json
from functools import lru_cache
from pathlib import Path
from typing import Tuple

DATA_DIR = Path(__file__).parent / "data"
CLOSE_MATCH_CUTOFF = 0.85


@lru_cache(maxsize=1)
def known_services() -> Tuple[str, ...]:
    """:return: every service name that appears in the logs or deploy datasets (lower-case)."""
    names = set()
    for file_name in ("logs.json", "deploys.json"):
        for entry in json.loads((DATA_DIR / file_name).read_text(encoding="utf-8")):
            names.add(entry["service"].lower())
    return tuple(sorted(names))


def canonical_service_name(name: str) -> str:
    """
    Maps a model-supplied service name onto the real one when it has drifted slightly.

    When an incident report does not name the service, the model tends to guess ("payment-gateway"
    for "payments-gateway"); an exact-match lookup then silently finds nothing and the whole triage
    degrades. Prompt rules only reduce this, so the tools resolve the name themselves. Unknown
    names (e.g. a service that genuinely has no data) are returned unchanged.

    :param name: the service name as the model wrote it.
    :return: the closest known service name (lower-case) if one is very close, else the cleaned input.
    """
    cleaned = (name or "").strip().lower()
    if not cleaned or cleaned in known_services():
        return cleaned
    matches = difflib.get_close_matches(cleaned, known_services(), n=1, cutoff=CLOSE_MATCH_CUTOFF)
    return matches[0] if matches else cleaned
