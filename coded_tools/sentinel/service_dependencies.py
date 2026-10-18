"""Deterministic, static service dependency graph used to chase a root cause across services."""

import json
from functools import lru_cache
from pathlib import Path
from typing import List
from typing import Tuple

from coded_tools.sentinel.service_names import canonical_service_name

DATA_FILE = Path(__file__).parent / "data" / "service_dependencies.json"


@lru_cache(maxsize=1)
def _graph() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    """:return: the static service -> upstream-services map, loaded once."""
    raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return tuple((service.lower(), tuple(dep.lower() for dep in deps)) for service, deps in raw.items())


def upstream_of(service_name: str) -> List[str]:
    """
    :param service_name: the service whose dependencies should be looked up.
    :return: the services ``service_name`` calls (in order), or an empty list if none are known.
    """
    canonical = canonical_service_name(service_name)
    for service, deps in _graph():
        if service == canonical:
            return list(deps)
    return []
