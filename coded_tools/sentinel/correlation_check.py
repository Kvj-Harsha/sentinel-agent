"""
Coded tool implementing the evaluation loop: verifies that a proposed root-cause
hypothesis is actually supported by both the logs evidence and the deploy-timeline
evidence, before the Scribe agent is allowed to present it with confidence.

It re-derives the incident timestamp from normalized evidence published by the deterministic
LogsAgent tool, and the deploy timestamp from the deploy source, rather than trusting timestamp
strings relayed through several LLM-to-LLM hops. A small model can paraphrase or invent a time;
using the tool-published source records keeps the evaluation loop independent of agent prose.
"""

import logging
from datetime import datetime
from typing import Any
from typing import Dict
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel.service_names import canonical_service_name

from coded_tools.sentinel.ticket_notes import add_note
from coded_tools.sentinel.schemas import redact_text

from coded_tools.sentinel.verified_facts import publish

logger = logging.getLogger(__name__)

# A deploy occurring more than this many minutes before the first anomaly is
# considered too distant in time to be confidently implicated as the cause.
MAX_SUPPORTING_WINDOW_MINUTES = 30


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _earliest_anomaly(service_name: str, sly_data: Dict[str, Any]) -> Dict[str, Any]:
    """Use timestamps published by the deterministic log tool, never LLM-relayed prose."""
    evidence = (sly_data.get("sentinel_verified") or {}).get("evidence") or []
    anomalies = [
        record for record in evidence
        if record.get("evidence_type") == "log"
        and str(record.get("service_name") or "").lower() == service_name
        and record.get("event_time")
        and record.get("source_status") in {"live", "fixture"}
    ]
    anomalies.sort(key=lambda record: record["event_time"])
    if not anomalies:
        return None
    first = anomalies[0]
    fields = first.get("safe_fields") or {}
    labels = fields.get("labels") or {}
    return {
        "timestamp": first["event_time"],
        "source": first.get("source", "unknown"),
        "source_status": first.get("source_status", "unknown"),
        "environment": fields.get("environment") or labels.get("environment"),
    }


def _most_recent_deploy(service_name: str, environment: str, sly_data: Dict[str, Any]) -> Dict[str, Any]:
    """Select a successful deployment from source-published evidence, never fixture files directly."""
    verified = sly_data.get("sentinel_verified") or {}
    evidence = verified.get("evidence") or []
    lookup = verified.get("deployment_lookup") or {}
    explicit_live_source = lookup.get("source") == "github:deployments"
    candidates = []
    for record in evidence:
        if str(record.get("service_name") or "").lower() != service_name:
            continue
        fields = record.get("safe_fields") or {}
        status = str(record.get("source_status") or "")
        evidence_type = record.get("evidence_type")
        if status == "live":
            if evidence_type != "deployment" or str(fields.get("state") or "").lower() != "success":
                continue
            deploy_environment = str(fields.get("environment") or "")
            if not environment or deploy_environment.casefold() != environment.casefold():
                continue
        elif status == "fixture":
            if explicit_live_source:
                continue
            if evidence_type != "deployment_change":
                continue
            deploy_environment = str(fields.get("environment") or "")
            if environment and deploy_environment and deploy_environment.casefold() != environment.casefold():
                continue
        else:
            continue
        if record.get("event_time"):
            candidates.append(record)
    candidates.sort(key=lambda record: record["event_time"], reverse=True)
    if not candidates:
        return None
    selected = candidates[0]
    fields = selected.get("safe_fields") or {}
    return {
        "deployed_at": selected["event_time"],
        "version": fields.get("commit_sha") or fields.get("version"),
        "service": selected.get("service_name"),
        "environment": fields.get("environment"),
        "source": selected.get("source"),
        "source_status": selected.get("source_status"),
        "source_url": selected.get("source_url"),
        "state": fields.get("state") or "fixture",
    }


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
        hypothesis: str = redact_text((args.get("hypothesis") or "").strip())

        missing = [name for name, val in (("service_name", service_name), ("hypothesis", hypothesis)) if not val]
        if missing:
            return f"Error: Missing required argument(s): {', '.join(missing)}."

        anomaly = _earliest_anomaly(service_name, sly_data)
        lookup = (sly_data.get("sentinel_verified") or {}).get("deployment_lookup") or {}
        # Log lines often carry no environment label; fall back to the environment the deployment lookup was
        # queried with (taken from the incident), otherwise a live deployment would never match.
        environment = (anomaly.get("environment") if anomaly else "") or lookup.get("environment") or ""
        deploy = _most_recent_deploy(service_name, environment, sly_data)

        if anomaly is None or deploy is None:
            if deploy is None and lookup.get("service_name") == service_name:
                lookup_status = lookup.get("source_status", "unknown")
                lookup_env = lookup.get("environment")
                if lookup_status == "live" and int(lookup.get("record_count", 0) or 0) == 0:
                    reason = f"GitHub returned no deployment records for '{service_name}' in '{lookup_env}'"
                elif lookup_status == "live":
                    reason = f"No successful deployment in '{lookup_env}' matches the incident environment and service"
                else:
                    reason = f"Deployment evidence from {lookup.get('source', 'the configured source')} is unavailable"
            else:
                lookup_status = "unknown"
                reason = "Could not find both an anomalous log entry and a successful deployment record"
            result = {
                "verdict": "NOT_SUPPORTED",
                "insufficient_evidence": True,
                "deployment_source_status": lookup_status,
                "reasoning": f"{reason} for service '{service_name}'; insufficient data to support a root-cause hypothesis.",
                "service_name": service_name,
                "hypothesis": hypothesis,
            }
            publish(sly_data, "correlation", result)
            return result

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
            "environment": deploy.get("environment") or anomaly.get("environment"),
            "incident_source": anomaly["source"],
            "incident_source_status": anomaly["source_status"],
            "deployment_source": deploy.get("source"),
            "deployment_source_status": deploy.get("source_status"),
            "deployment_url": deploy.get("source_url"),
            "reasoning": reasoning,
        }
        previous = (sly_data.get("sentinel_verified") or {}).get("correlation") or {}
        keep_previous = previous.get("service_name") == service_name and previous.get("verdict") == "SUPPORTED"
        if not keep_previous:
            publish(sly_data, "correlation", {**result, "service_name": service_name, "hypothesis": hypothesis})
        if not keep_previous:
            add_note(sly_data, f"[Root cause] Correlation check {verdict}: {reasoning}",
                     once_key=f"correlation:{service_name}:{verdict}")
        return result

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick, non-blocking."""
        return self.invoke(args, sly_data)
