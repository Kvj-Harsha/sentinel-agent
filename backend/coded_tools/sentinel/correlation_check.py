"""
Coded tool implementing the evaluation loop: verifies that a proposed root-cause
hypothesis is actually supported by both the logs evidence and the deploy-timeline
evidence, before the Scribe agent is allowed to present it with confidence.

Deliberately self-contained: it re-derives the incident and deploy timestamps itself
from the same synthetic datasets LogsAgent/DeployHistoryAgent query, rather than trusting
timestamp strings relayed through several LLM-to-LLM hops. In testing, a small model would
occasionally paraphrase or invent a slightly-wrong timestamp by the time it reached this
tool several hops downstream of LogsAgent/DeployHistoryAgent -- re-deriving from source
data makes the evaluation loop's verdict correct regardless of what the agents say to
each other in natural language.
"""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name

from coded_tools.sentinel.logs_lookup import LogsLookup
from coded_tools.sentinel.ticket_notes import add_note

from coded_tools.sentinel.verified_facts import publish

logger = logging.getLogger(__name__)

LOGS_FILE = Path(__file__).parent / "data" / "logs.json"
DEPLOYS_FILE = Path(__file__).parent / "data" / "deploys.json"

ANOMALY_LEVELS = {"ERROR", "WARN"}

# A deploy occurring more than this many minutes before the first anomaly is
# considered too distant in time to be confidently implicated as the cause.
MAX_SUPPORTING_WINDOW_MINUTES = 30


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _earliest_anomaly(service_name: str) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = json.loads(LOGS_FILE.read_text(encoding="utf-8"))
    anomalies = [
        e
        for e in entries
        if service_name in e["service"].lower()
        and (e.get("level") in ANOMALY_LEVELS or e.get("status_code", 0) >= 400)
    ]
    anomalies.sort(key=lambda e: e["timestamp"])
    return anomalies[0] if anomalies else None


def _most_recent_deploy(service_name: str) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = json.loads(DEPLOYS_FILE.read_text(encoding="utf-8"))
    deploys = [e for e in entries if service_name in e["service"].lower()]
    deploys.sort(key=lambda e: e["deployed_at"], reverse=True)
    return deploys[0] if deploys else None


class CorrelationCheck(CodedTool):
    """
    Checks whether the most recent deploy to a service plausibly explains that service's
    earliest logged anomaly, and whether the stated hypothesis actually references the
    service in question. This is the self-check / evaluation-loop step: it exists to stop
    the Scribe agent from presenting a confident root cause that the underlying data does
    not actually support.
    """

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: a dictionary with the following keys:
            - service_name: the service the incident is about.
            - hypothesis: the proposed root-cause sentence to sanity-check.

        :param sly_data: the verdict is published to sly_data["sentinel_verified"]["correlation"].

        :return: a dict with a "verdict" of SUPPORTED or NOT_SUPPORTED, plus "reasoning"
            and the incident_time/deploy_time this check actually used.
        """
        raw_service_name: str = (args.get("service_name") or "").strip().lower()
        service_name: str = canonical_service_name(raw_service_name)
        hypothesis: str = (args.get("hypothesis") or "").strip()

        missing = [name for name, val in (("service_name", service_name), ("hypothesis", hypothesis)) if not val]
        if missing:
            return f"Error: Missing required argument(s): {', '.join(missing)}."

        anomaly = _earliest_anomaly(service_name)
        deploy = _most_recent_deploy(service_name)

        if anomaly is None or deploy is None:
            return {
                "verdict": "NOT_SUPPORTED",
                "reasoning": (
                    f"Could not find both an anomalous log entry and a deploy record for "
                    f"service '{service_name}' -- insufficient data to support any hypothesis."
                ),
            }

        incident_time, deploy_time = anomaly["timestamp"], deploy["deployed_at"]
        incident_dt, deploy_dt = _parse(incident_time), _parse(deploy_time)
        delta_minutes = (incident_dt - deploy_dt).total_seconds() / 60.0

        if service_name not in hypothesis.lower() and raw_service_name not in hypothesis.lower():
            verdict = "NOT_SUPPORTED"
            reasoning = (
                f"The hypothesis does not mention the affected service ('{service_name}'), "
                "so it cannot be considered a service-specific root cause."
            )
        elif delta_minutes < 0:
            verdict = "NOT_SUPPORTED"
            reasoning = (
                f"The deploy at {deploy_time} happened AFTER the incident started at {incident_time} "
                f"({abs(delta_minutes):.1f} minutes later), so it cannot be the cause."
            )
        elif delta_minutes > MAX_SUPPORTING_WINDOW_MINUTES:
            verdict = "NOT_SUPPORTED"
            reasoning = (
                f"The deploy at {deploy_time} happened {delta_minutes:.1f} minutes before the incident at "
                f"{incident_time}, which is beyond the {MAX_SUPPORTING_WINDOW_MINUTES}-minute window normally "
                "considered a plausible trigger. Treat this as a weak/unconfirmed correlation."
            )
        else:
            verdict = "SUPPORTED"
            reasoning = (
                f"The deploy at {deploy_time} happened {delta_minutes:.1f} minutes before the incident at "
                f"{incident_time} on the same service ('{service_name}'), which is consistent with the "
                "hypothesis. This is still a correlation, not proof -- state it as a leading hypothesis."
            )

        logger.debug("CorrelationCheck verdict=%s delta_minutes=%.1f", verdict, delta_minutes)
        result = {
            "verdict": verdict,
            "delta_minutes": round(delta_minutes, 1),
            "incident_time": incident_time,
            "deploy_time": deploy_time,
            "reasoning": reasoning,
        }
        previous = (sly_data.get("sentinel_verified") or {}).get("correlation") or {}
        keep_previous = previous.get("service_name") == service_name and previous.get("verdict") == "SUPPORTED"
        if not keep_previous:
            publish(sly_data, "correlation", {**result, "service_name": service_name, "hypothesis": hypothesis})
        if not keep_previous:
            # The model may skip the LogsAgent; the evidence a root cause rests on must still be on the ticket
            # (the note is posted once per service, so this is a no-op if logs_lookup already posted it).
            LogsLookup().invoke({"service_name": service_name}, sly_data)
            add_note(sly_data, f"[Root cause] Correlation check {verdict}: {reasoning}",
                     once_key=f"correlation:{service_name}:{verdict}")
        return result

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
