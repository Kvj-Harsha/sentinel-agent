"""Coded tool that generates a config fix from the runbook and runs a verification pipeline over it."""

import difflib
import json
import logging
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

import yaml
from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel import ticket_notes
from coded_tools.sentinel.service_names import canonical_service_name
from coded_tools.sentinel.ticket_store import TicketStore
from coded_tools.sentinel.verified_facts import VERIFIED_KEY
from coded_tools.sentinel.verified_facts import publish

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_PLAN_FILE = DATA_DIR / "remediation.json"
MAX_DIFF_LINES_IN_NOTE = 14
ALLOWED_CONFIG_FILES = {"checkout-service": "configs/checkout-service.yaml"}
ALLOWED_PATCH_TYPES = {
    "checkout-service": {
        "tax.fallback_to_legacy_table": bool,
        "tax.null_rate_guard": bool,
    },
}
SECRET_PATTERNS = {
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "provider or service token": r"(?i)\b(?:sk-(?:or-v1-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|ATATT[A-Za-z0-9_+/=-]{20,}|eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})\b",
    "hard-coded credential": r"(?i)\b(?:password|passwd|pwd|api[_-]?key|access[_-]?token|client[_-]?secret)\s*[:=]\s*\S+",
}


def load_plans(plan_file: Path = DEFAULT_PLAN_FILE) -> Dict[str, Any]:
    """:return: the remediation plans keyed by service name."""
    return json.loads(plan_file.read_text(encoding="utf-8"))


def plan_exists(service_name: str, plan_file: Path = DEFAULT_PLAN_FILE) -> bool:
    """:return: True when an automated remediation plan is defined for the service."""
    return canonical_service_name(service_name) in load_plans(plan_file)


def _apply_patches(config: Dict[str, Any], patches: List[Dict[str, Any]]) -> Dict[str, Any]:
    patched = deepcopy(config)
    for patch in patches:
        *parents, leaf = patch["path"].split(".")
        node = patched
        for key in parents:
            node = node[key]
        node[leaf] = patch["value"]
    return patched


class RemediationPipeline(CodedTool):
    """
    After a SUPPORTED root cause, builds the fix from the service's config file and verifies it the way a CI
    pipeline would, posting every step to the incident ticket (and so to ServiceNow's activity stream).

    This produces a proposed config diff and runs local preflight checks. It does not edit a running service,
    create a GitHub PR, or claim CI/canary results. The separate human-in-the-loop UI owns those steps.
    """

    def __init__(self, plan_file: Optional[Path] = None) -> None:
        """:param plan_file: remediation plans file; tests inject a temporary one."""
        self.plan_file = plan_file or DEFAULT_PLAN_FILE

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: service_name: the affected service.
        :param sly_data: requires a SUPPORTED correlation verdict; publishes "remediation" facts and posts notes.
        :return: the fix, every check's result and an overall all_passed flag, or an error string.
        """
        service_name = canonical_service_name(args.get("service_name") or "")
        if not service_name:
            return "Error: Missing required argument(s): service_name."
        plans = load_plans(self.plan_file)
        if service_name not in plans:
            return {"applicable": False, "summary": f"No automated remediation plan is defined for '{service_name}'."}

        verdict = ((sly_data.get(VERIFIED_KEY) or {}).get("correlation") or {}).get("verdict")
        if verdict != "SUPPORTED":
            return "Error: Refusing to remediate: the root cause is not verified (correlation_check is not SUPPORTED)."
        correlation = (sly_data.get(VERIFIED_KEY) or {}).get("correlation") or {}
        if correlation.get("service_name") != service_name:
            return "Error: Refusing to remediate: the verified correlation belongs to a different service."

        existing = (sly_data.get(VERIFIED_KEY) or {}).get("remediation")
        if existing:
            self._persist_repair_binding(sly_data, service_name, plans[service_name], existing)
            return {**existing, "summary": "Remediation already ran for this incident; not repeating it."}

        plan = plans[service_name]
        allowed_config = ALLOWED_CONFIG_FILES.get(service_name)
        if not allowed_config or plan.get("config_file") != allowed_config:
            return "Error: Refusing to remediate: the configured file is outside the service allow-list."
        allowed_patches = ALLOWED_PATCH_TYPES.get(service_name, {})
        for patch in plan.get("patches", []):
            expected_type = allowed_patches.get(patch.get("path"))
            if expected_type is None or type(patch.get("value")) is not expected_type:
                return "Error: Refusing to remediate: the patch path or value is outside the service allow-list."
        config_path = (DATA_DIR / allowed_config).resolve()
        if DATA_DIR.resolve() not in config_path.parents:
            return "Error: Refusing to remediate: the configured file escapes the local data directory."
        original = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(original, dict):
            return "Error: Refusing to remediate: the allow-listed config is not a YAML object."
        patched = _apply_patches(original, plan["patches"])
        patched_text = yaml.safe_dump(patched, sort_keys=False)
        diff = list(
            difflib.unified_diff(
                yaml.safe_dump(original, sort_keys=False).splitlines(),
                patched_text.splitlines(),
                fromfile=f"a/{plan['config_file']}",
                tofile=f"b/{plan['config_file']}",
                lineterm="",
            )
        )

        ticket_notes.add_note(
            sly_data,
            f"[Remediation] Fix generated from runbook {plan['runbook_id']}: {plan['fix_summary']}\n"
            + "\n".join(diff[:MAX_DIFF_LINES_IN_NOTE]),
        )

        checks = [
            self._schema_check(patched, plan),
            self._secret_scan(patched_text),
            self._dependency_audit(patched, plan),
        ]
        for check in checks:
            mark = "PASSED" if check["status"] == "passed" else "FAILED"
            ticket_notes.add_note(sly_data, f"[Preflight] {check['name']}: {mark} - {check['detail']}")

        all_passed = all(check["status"] == "passed" for check in checks)
        facts = {
            "service_name": service_name,
            "runbook_id": plan["runbook_id"],
            "fix_summary": plan["fix_summary"],
            "config_file": plan["config_file"],
            "diff": diff,
            "checks": checks,
            "all_passed": all_passed,
        }
        publish(sly_data, "remediation", facts)
        self._persist_repair_binding(sly_data, service_name, plan, facts)
        outcome = "local preflight passed; open a GitHub PR for CI and human review" if all_passed else "preflight FAILED; do not proceed"
        ticket_notes.add_note(sly_data, f"[Remediation proposal] {outcome}.")
        return {**facts, "summary": f"Fix proposed only; {outcome}. No running service was changed."}

    @staticmethod
    def _persist_repair_binding(
        sly_data: Dict[str, Any], service_name: str, plan: Dict[str, Any], facts: Dict[str, Any],
        patch: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a compact incident/evidence binding for the separate approval UI."""
        ticket_number = sly_data.get("sentinel_ticket_number")
        session_id = sly_data.get("incident_session_id")
        if not ticket_number or not session_id:
            return
        verified = sly_data.get(VERIFIED_KEY) or {}
        correlation = verified.get("correlation") or {}
        evidence = verified.get("evidence") or []
        refs = []
        for item in evidence:
            if (item.get("service_name") or "").casefold() != service_name.casefold():
                continue
            if item.get("source_status") not in {"live", "fixture"}:
                continue
            if item.get("evidence_type") not in {"log", "deployment", "deployment_change"}:
                continue
            record_id = item.get("record_id") or item.get("event_time")
            if record_id:
                refs.append({
                    "source": item.get("source"), "record_id": str(record_id),
                    "evidence_type": item.get("evidence_type"), "source_status": item.get("source_status"),
                })
        refs = list({(item["source"], item["record_id"], item["evidence_type"]): item for item in refs}.values())
        log_refs = [item for item in refs if item["evidence_type"] == "log"]
        deploy_refs = [item for item in refs if item["evidence_type"] in {"deployment", "deployment_change"}]
        binding = {
            "ticket_number": ticket_number,
            "session_id": session_id,
            "service_name": service_name,
            "verdict": correlation.get("verdict"),
            "runbook_id": plan.get("runbook_id"),
            "evidence_refs": refs,
            "all_passed": bool(facts.get("all_passed")),
            "eligible": bool(
                facts.get("all_passed") and correlation.get("verdict") == "SUPPORTED"
                and correlation.get("service_name") == service_name and log_refs and deploy_refs
            ),
        }
        if patch:
            # An LLM-written code patch: the PR must contain exactly this content (verified by hash).
            binding["patch"] = {key: patch[key] for key in ("path", "content", "content_sha256", "base_sha256", "summary")}
        store = TicketStore()
        ticket = store.get(ticket_number)
        if ticket is None or ticket.get("session_id") != session_id:
            return
        store.update(ticket_number, {"repair_context": binding})
        store.append_audit_event(
            "repair.binding", "ticket", ticket_number,
            fingerprint=ticket.get("alert_fingerprint"), session_id=session_id,
            details={
                "service_name": service_name,
                "runbook_id": plan.get("runbook_id"),
                "decision": "allow_pr" if binding["eligible"] else "block_pr",
                "outcome": "eligible" if binding["eligible"] else "ineligible",
                "verdict": binding.get("verdict"),
                "evidence_ids": ",".join(ref["record_id"] for ref in refs)[:500],
            },
        )

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick."""
        return self.invoke(args, sly_data)

    @staticmethod
    def _schema_check(patched: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        missing = [key for key in plan["required_config_keys"] if key not in patched]
        tax = patched.get("tax")
        valid_types = (
            isinstance(tax, dict)
            and type(tax.get("fallback_to_legacy_table")) is bool
            and type(tax.get("null_rate_guard")) is bool
            and isinstance(tax.get("library"), str)
            and bool(tax.get("library"))
            and isinstance(tax.get("library_version"), (str, int, float))
        )
        ok = not missing and valid_types
        detail = "patched config has all required keys and valid types" if ok else f"missing keys: {missing}; valid types: {valid_types}"
        return {"name": "Config schema validation", "status": "passed" if ok else "failed",
                "source": "real", "detail": detail}

    @staticmethod
    def _secret_scan(patched_text: str) -> Dict[str, Any]:
        found = [label for label, pattern in SECRET_PATTERNS.items() if re.search(pattern, patched_text)]
        detail = "no secrets detected in the patched config" if not found else f"possible secrets: {found}"
        return {"name": "Secret scanning", "status": "failed" if found else "passed", "source": "real",
                "detail": detail}

    @staticmethod
    def _dependency_audit(patched: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        library, version = patched["tax"]["library"], str(patched["tax"]["library_version"])
        hits = [a for a in plan["dependency_advisories"] if a["package"] == library and version in a["affected_versions"]]
        detail = (f"{library}@{version}: no known advisories" if not hits
                  else f"{library}@{version} affected by {', '.join(a['id'] for a in hits)}")
        return {"name": "Dependency audit", "status": "failed" if hits else "passed", "source": "real",
                "detail": detail}
