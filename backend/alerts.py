"""
Alert front door for Sentinel.

A real on-call engineer does not hand-write a precise incident description in the first minute of an
outage; an alert arrives as structured data (service, host, symptom, timestamp) from a monitoring tool
such as Datadog. This module accepts exactly that shape and deterministically (plain Python, no LLM)
composes the incident report the agent network then triages.

Run with:  python backend/alerts.py --preset checkout-500
      or:  python backend/alerts.py --payload path/to/alert.json
"""

import argparse
import json
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List

from coded_tools.sentinel.schemas import Incident
from coded_tools.sentinel.schemas import dump_model

SAMPLE_ALERTS_DIR = Path(__file__).resolve().parent / "sample_alerts"


def list_presets() -> List[str]:
    """:return: the names of the built-in sample alerts (files under backend/sample_alerts/)."""
    return sorted(path.stem for path in SAMPLE_ALERTS_DIR.glob("*.json"))


def load_preset(name: str) -> Dict[str, Any]:
    """
    :param name: a preset name from list_presets().
    :return: the alert payload (Datadog-monitor-alert shaped dict).
    """
    path = SAMPLE_ALERTS_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Unknown alert preset '{name}'. Available: {', '.join(list_presets())}")
    return json.loads(path.read_text(encoding="utf-8"))


def compose_incident_report(alert: Dict[str, Any]) -> str:
    """
    Deterministically builds a plain incident-report string from an alert payload (no LLM involved).

    :param alert: a dict with "title", "body", "host", "tags" (a "service:<name>" tag is required)
        and "date".
    :return: a single incident-report string ready to send to the IncidentCommander agent.
    """
    normalized = alert.get("normalized_incident")
    if normalized:
        incident = Incident.model_validate(normalized)
    else:
        service_tags = [tag.split(":", 1)[1] for tag in alert.get("tags", []) if tag.startswith("service:")]
        if not service_tags:
            raise ValueError("Alert payload is missing a 'service:<name>' tag; cannot identify the affected service.")
        incident = Incident(
            incident_id=alert.get("incident_id"),
            fingerprint=alert.get("fingerprint"),
            service_name=service_tags[0],
            environment=next((tag.split(":", 1)[1] for tag in alert.get("tags", []) if tag.startswith("env:")), None),
            title=alert.get("title") or "Alert",
            description=alert.get("body") or "",
            source=alert.get("source") or "unknown",
            fired_at=alert.get("date") if alert.get("date") not in (None, "", "unknown time") else None,
            host=alert.get("host"),
        )

    environment = f" Environment: {incident.environment}." if incident.environment else ""
    return (
        f"{incident.title} "
        f"Affected service: {incident.service_name}."
        f"{environment} "
        f"{incident.description.strip()} "
        f"(host: {incident.host or 'unknown'}, alert fired at {incident.fired_at.isoformat() if incident.fired_at else 'unknown time'})"
    ).strip()


def compose_alertmanager_alert(alert: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one Prometheus Alertmanager alert into Sentinel's normalized alert shape.

    :param alert: one element from Alertmanager's ``alerts`` array.
    :return: normalized alert accepted by :func:`compose_incident_report`.
    :raises ValueError: when the alert is resolved or has no identifiable service.
    """
    labels = alert.get("labels") or {}
    annotations = alert.get("annotations") or {}
    state = str(alert.get("status") or "firing").lower()
    if state != "firing":
        raise ValueError(f"Ignoring non-firing Alertmanager event (status={state}).")

    service_name = next(
        (str(labels[key]).strip() for key in ("service", "service_name", "app") if labels.get(key)),
        "",
    )
    if not service_name:
        raise ValueError("Alertmanager alert needs a 'service', 'service_name', or 'app' label.")

    title = str(annotations.get("summary") or labels.get("alertname") or "Prometheus alert").strip()
    description = str(annotations.get("description") or annotations.get("message") or "").strip()
    details = [f"Severity: {labels.get('severity', 'unspecified')}."]
    if description:
        details.append(description)
    if alert.get("generatorURL"):
        details.append(f"Prometheus expression: {alert['generatorURL']}")

    safe_label_keys = ("alertname", "service", "service_name", "app", "environment", "env", "severity")
    safe_labels = {key: str(labels[key]) for key in safe_label_keys if labels.get(key) is not None}
    incident = Incident(
        incident_id=str(alert.get("fingerprint") or "") or None,
        fingerprint=str(alert.get("fingerprint") or "") or None,
        service_name=service_name,
        environment=str(labels.get("environment") or labels.get("env") or "") or None,
        severity=str(labels.get("severity") or "") or None,
        title=title,
        description=" ".join(details),
        source="Prometheus Alertmanager",
        fired_at=alert.get("startsAt") or None,
        host=str(labels.get("instance") or labels.get("host") or "") or None,
        labels=safe_labels,
        source_url=alert.get("generatorURL") or None,
        source_payload_ref=str(alert.get("fingerprint") or "") or None,
    )
    return {
        "title": title,
        "body": " ".join(details),
        "host": labels.get("instance") or labels.get("host") or "unknown",
        "tags": [f"service:{service_name}"],
        "date": alert.get("startsAt") or "unknown time",
        "source": "Prometheus Alertmanager",
        "fingerprint": alert.get("fingerprint", ""),
        "normalized_incident": dump_model(incident),
    }


def main() -> None:
    """CLI: compose an incident report from a preset/payload and triage it with the agent network."""
    # Imported here so that importing this module (e.g. from tests) has no side effects.
    from run import format_verified_facts  # pylint: disable=import-outside-toplevel
    from run import invoke_agent  # pylint: disable=import-outside-toplevel
    from run import make_stdout_safe  # pylint: disable=import-outside-toplevel

    make_stdout_safe()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--preset", choices=list_presets(), help="Built-in sample alert (backend/sample_alerts/).")
    group.add_argument("--payload", help="Path to a custom Datadog-monitor-alert-shaped JSON file.")
    args = parser.parse_args()

    if args.payload:
        alert = json.loads(Path(args.payload).read_text(encoding="utf-8"))
    else:
        alert = load_preset(args.preset)

    report = compose_incident_report(alert)
    print(f"Composed incident report:\n  {report}\n")
    response = invoke_agent(report)
    print(response.get("text", "(no response)"))
    verified = format_verified_facts(response.get("sly_data"))
    if verified:
        print("\nVerified by tools (deterministic, not LLM-generated):\n" + verified)


if __name__ == "__main__":
    main()
