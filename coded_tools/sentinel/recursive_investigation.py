"""
Recursive multi-hop investigation: when a service's incident is NOT_SUPPORTED by direct evidence,
chase the same investigate -> correlate loop onto the service it depends on, in case the real root
cause lives one (or a few) hops upstream. Each hop is a complete, independently ticketed
investigation; recursion stops as soon as a hop comes back SUPPORTED, the depth cap is hit, or
there is no unvisited dependency left to chase (cycles are hard no-ops, never infinite loops).

This lives at the orchestration layer, not inside the HOCON agent instructions: the agent network
itself (IncidentCommander -> specialists -> Scribe) only ever reasons about one service per call, by
design (see correlation_check.py). The recursion -- deciding whether and what to investigate next --
is deterministic control flow, consistent with this repo's "LLMs route and explain, coded tools
decide" principle.
"""

import logging
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Tuple

from coded_tools.sentinel.service_dependencies import upstream_of

logger = logging.getLogger(__name__)

InvestigateOne = Callable[[str], Tuple[Dict[str, Any], Dict[str, Any]]]


def run_investigation_chain(
    investigate_one: InvestigateOne,
    service_name: str,
    max_depth: int = 2,
) -> Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]:
    """
    :param investigate_one: runs one normal, single-service investigation (open ticket + invoke_agent,
        exactly what the call site already does) and returns (sly_data, response) for that service.
    :param service_name: the service named by the triggering alert.
    :param max_depth: the most additional hops to chase beyond the first (bounded so a bad dependency
        map or a stuck loop can never run away).
    :return: (final_sly_data, final_response, chain) where chain is an ordered list of
        {"service_name", "ticket_number", "verdict", "reasoning"} dicts, one per hop actually run.
    """
    visited = set()
    chain: List[Dict[str, Any]] = []
    current_service = service_name
    sly_data: Dict[str, Any] = {}
    response: Dict[str, Any] = {}
    depth = 0

    while True:
        visited.add(current_service.strip().lower())
        sly_data, response = investigate_one(current_service)
        correlation = (sly_data.get("sentinel_verified") or {}).get("correlation") or {}
        verdict = correlation.get("verdict")
        chain.append({
            "service_name": current_service,
            "ticket_number": sly_data.get("sentinel_ticket_number"),
            "verdict": verdict,
            "reasoning": correlation.get("reasoning"),
        })
        # Only chase a dependency on an explicit NOT_SUPPORTED verdict -- an unknown/missing verdict (the
        # correlation check never ran, e.g. the investigation errored before reaching it) is not evidence
        # that the real cause lies upstream, so it must not trigger recursion either.
        if verdict != "NOT_SUPPORTED" or depth >= max_depth:
            break
        candidates = [dep for dep in upstream_of(current_service) if dep.strip().lower() not in visited]
        if not candidates:
            break
        logger.info("Recursive investigation: %s was NOT_SUPPORTED, chasing dependency %s (hop %d)",
                    current_service, candidates[0], depth + 1)
        current_service = candidates[0]
        depth += 1

    return sly_data, response, chain


def summarize_chain(chain: List[Dict[str, Any]]) -> str:
    """:return: a short, human-readable one-liner describing a multi-hop chain, for a ticket work note."""
    if len(chain) <= 1:
        return ""
    hops = " -> ".join(f"{hop['service_name']} ({hop.get('verdict') or 'unknown'})" for hop in chain)
    return f"Recursive dependency chase: {hops}"
