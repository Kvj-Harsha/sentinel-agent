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
from coded_tools.sentinel.verified_facts import VERIFIED_KEY
from coded_tools.sentinel.verified_facts import publish

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent / "data"
DEFAULT_PLAN_FILE = DATA_DIR / "remediation.json"
MAX_DIFF_LINES_IN_NOTE = 14
SECRET_PATTERNS = {
    "AWS access key": r"AKIA[0-9A-Z]{16}",
    "private key block": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "OpenAI-style secret": r"sk-[A-Za-z0-9]{20,}",
    "hard-coded password": r"(?i)password\s*[:=]\s*\S+",
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

        existing = (sly_data.get(VERIFIED_KEY) or {}).get("remediation")
        if existing:
            return {**existing, "summary": "Remediation already ran for this incident; not repeating it."}

        plan = plans[service_name]
        original = yaml.safe_load((DATA_DIR / plan["config_file"]).read_text(encoding="utf-8"))
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
        outcome = "local preflight passed; open a GitHub PR for CI and human review" if all_passed else "preflight FAILED; do not proceed"
        ticket_notes.add_note(sly_data, f"[Remediation proposal] {outcome}.")
        return {**facts, "summary": f"Fix proposed only; {outcome}. No running service was changed."}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """Delegates to the synchronous invoke method because it's quick."""
        return self.invoke(args, sly_data)

    @staticmethod
    def _schema_check(patched: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        missing = [key for key in plan["required_config_keys"] if key not in patched]
        ok = not missing and isinstance(patched["tax"].get("fallback_to_legacy_table"), bool)
        detail = "patched config has all required keys and valid types" if ok else f"missing keys: {missing}"
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
