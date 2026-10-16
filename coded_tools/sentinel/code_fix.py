"""FixAgent tools: read the failing source, then submit an LLM-written patch that code verifies before any PR exists.

The model proposes; deterministic checks decide. A patch is only eligible for a pull request when it passes every
check below. A human still reviews and merges the PR, and CI runs on it.
"""

import ast
import difflib
import hashlib
import json
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Union

from neuro_san.interfaces.coded_tool import CodedTool

from coded_tools.sentinel import github_pr_client
from coded_tools.sentinel import ticket_notes
from coded_tools.sentinel.mode import demo_mode
from coded_tools.sentinel.remediation_pipeline import SECRET_PATTERNS
from coded_tools.sentinel.remediation_pipeline import RemediationPipeline
from coded_tools.sentinel.schemas import redact_text
from coded_tools.sentinel.service_names import canonical_service_name
from coded_tools.sentinel.verified_facts import VERIFIED_KEY
from coded_tools.sentinel.verified_facts import publish

LOGGER = logging.getLogger(__name__)
ORIGINAL_KEY = "sentinel_code_fix_original"
ALLOWED_FIX_PATHS = {"checkout-service": ("app/pricing.py",)}
SCENARIO_FILE = Path(__file__).parent / "data" / "scenario" / "pricing.py"
MAX_LINES = 120
MAX_CHANGED_LINES = 40
MAX_BYTES = 8000
SANDBOX_TIMEOUT_SECONDS = 10
ALLOWED_IMPORTS = {"decimal", "math", "typing", "functools", "fractions"}
FORBIDDEN_NAMES = {"exec", "eval", "compile", "open", "__import__", "input", "globals", "locals", "getattr", "setattr", "delattr", "vars"}
SUPPORTED_SAMPLES = [("US", 100), ("GB", 100), ("DE", 100), ("FR", 100), ("US", 19.99)]
FALLBACK_FAILING_MARKETS = ["IN", "BR"]

# Runs in a separate interpreter (-I: no site, no env, no user paths). It never touches the network or the real app.
SANDBOX_RUNNER = r"""
import json, math, sys
def load(path):
    module = {}
    exec(compile(open(path, encoding="utf-8").read(), path, "exec"), module)
    return module
original, candidate = load(sys.argv[1]), load(sys.argv[2])
spec = json.loads(sys.argv[3])
result = {"preserved": [], "reproduced": [], "fixed": []}
for country, subtotal in spec["supported"]:
    expected = original["order_total"](subtotal, country)
    actual = candidate["order_total"](subtotal, country)
    result["preserved"].append({"country": country, "ok": actual == expected, "expected": expected, "actual": actual})
for country in spec["failing"]:
    try:
        original["order_total"](100, country)
        result["reproduced"].append({"country": country, "reproduced": False})
    except Exception as exc:
        result["reproduced"].append({"country": country, "reproduced": True, "error": type(exc).__name__})
    try:
        value = candidate["order_total"](100, country)
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 100
        result["fixed"].append({"country": country, "ok": ok, "value": value if ok else None})
    except Exception as exc:
        result["fixed"].append({"country": country, "ok": False, "error": type(exc).__name__})
print(json.dumps(result))
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _failing_markets(sly_data: Dict[str, Any]) -> List[str]:
    """Markets taken from the real error log lines (country=XX), so the check reproduces the actual incident."""
    markets = []
    for record in (sly_data.get(VERIFIED_KEY) or {}).get("evidence") or []:
        if record.get("evidence_type") != "log":
            continue
        text = f"{record.get('summary', '')} {json.dumps(record.get('safe_fields') or {})}"
        markets += re.findall(r"country=([A-Z]{2})", text)
    unique = list(dict.fromkeys(markets))
    supported = {country for country, _ in SUPPORTED_SAMPLES}
    return [market for market in unique if market not in supported][:4] or FALLBACK_FAILING_MARKETS


def _read_original(path: str) -> str:
    """The failing source: from the demo repository in live mode, from the bundled scenario in demo mode."""
    if demo_mode():
        return SCENARIO_FILE.read_text(encoding="utf-8")
    return github_pr_client.file_at_ref(path, github_pr_client.default_branch())


class ReadSourceFile(CodedTool):
    """Reads one allow-listed source file so the FixAgent can see the failing code."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: path: repository-relative path (must be allow-listed for the service); service_name: optional.
        :param sly_data: stores the original text privately so submit_code_fix can diff against it.
        """
        service = canonical_service_name(args.get("service_name") or "checkout-service")
        path = str(args.get("path") or "").strip()
        if path not in ALLOWED_FIX_PATHS.get(service, ()):
            return f"Error: '{path}' is not a source file Sentinel may read or fix for {service}. Allowed: {list(ALLOWED_FIX_PATHS.get(service, ()))}."
        try:
            content = _read_original(path)
        except (github_pr_client.GithubPullRequestError, OSError) as exc:
            return f"Error: could not read {path}: {exc}"
        sly_data[ORIGINAL_KEY] = {"path": path, "content": content, "sha256": _sha256(content)}
        numbered = "\n".join(f"{index:>3}: {line}" for index, line in enumerate(content.splitlines(), start=1))
        return {
            "path": path,
            "sha256": _sha256(content),
            "source_status": "fixture" if demo_mode() else "live",
            "content": numbered,
            "instructions": "Reply by calling submit_code_fix with the COMPLETE corrected file (no line numbers).",
        }

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        return self.invoke(args, sly_data)


def _ast_findings(source: str) -> List[str]:
    findings = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            findings += [f"import {alias.name}" for alias in node.names if alias.name.split(".")[0] not in ALLOWED_IMPORTS]
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                findings.append(f"from {node.module} import ...")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            findings.append(f"use of {node.id}")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            findings.append(f"dunder attribute {node.attr}")
    return findings


def _top_level_signatures(source: str) -> Dict[str, int]:
    return {node.name: len(node.args.args) for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}


def _sandbox(original: str, candidate: str, failing: List[str]) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory() as directory:
        original_path, candidate_path = Path(directory, "original.py"), Path(directory, "candidate.py")
        original_path.write_text(original, encoding="utf-8")
        candidate_path.write_text(candidate, encoding="utf-8")
        spec = json.dumps({"supported": SUPPORTED_SAMPLES, "failing": failing})
        completed = subprocess.run(  # noqa: S603 - fixed interpreter and our own runner; candidate is data, not argv
            [sys.executable, "-I", "-c", SANDBOX_RUNNER, str(original_path), str(candidate_path), spec],
            capture_output=True, text=True, timeout=SANDBOX_TIMEOUT_SECONDS, cwd=directory, check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(redact_text(completed.stderr.strip().splitlines()[-1] if completed.stderr.strip() else "sandbox failed"))
    return json.loads(completed.stdout)


def run_checks(path: str, original: str, candidate: str, failing: List[str]) -> List[Dict[str, Any]]:
    """Every check a patch must pass. Each result is {name, status, source, detail}; nothing here trusts the model."""
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str) -> bool:
        checks.append({"name": name, "status": "passed" if ok else "failed", "source": "real", "detail": detail})
        return ok

    diff_lines = list(difflib.unified_diff(original.splitlines(), candidate.splitlines(), lineterm="", n=0))
    changed = sum(1 for line in diff_lines if line[:1] in "+-" and line[:3] not in ("+++", "---"))
    add("Size and diff limits", len(candidate.splitlines()) <= MAX_LINES and len(candidate.encode()) <= MAX_BYTES
        and 0 < changed <= MAX_CHANGED_LINES, f"{len(candidate.splitlines())} lines, {changed} changed (limit {MAX_CHANGED_LINES})")
    try:
        findings = _ast_findings(candidate)
    except SyntaxError as exc:
        add("Syntax and safety (AST)", False, f"does not parse: {exc.msg} (line {exc.lineno})")
        return checks
    add("Syntax and safety (AST)", not findings, "parses; no unsafe imports or calls" if not findings else f"forbidden: {', '.join(findings[:4])}")
    original_api, candidate_api = _top_level_signatures(original), _top_level_signatures(candidate)
    missing = [name for name, args in original_api.items() if candidate_api.get(name) != args]
    add("Public API preserved", not missing, "every original function keeps its name and arguments" if not missing else f"changed or removed: {missing}")
    secrets = [label for label, pattern in SECRET_PATTERNS.items() if re.search(pattern, candidate)]
    add("Secret scanning", not secrets, "no secrets detected" if not secrets else f"possible secrets: {secrets}")
    if not all(check["status"] == "passed" for check in checks):
        return checks
    try:
        result = _sandbox(original, candidate, failing)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        add("Sandbox behaviour test", False, f"could not run: {type(exc).__name__}: {exc}")
        return checks
    preserved_ok = all(item["ok"] for item in result["preserved"])
    reproduced = [item for item in result["reproduced"] if item["reproduced"]]
    fixed_ok = all(item["ok"] for item in result["fixed"])
    add("Behaviour preserved for supported markets", preserved_ok,
        f"{len(result['preserved'])} known-market totals identical to the original" if preserved_ok else "a supported market's total changed")
    add("Reproduces the incident on the original code", bool(reproduced),
        f"original raises for {', '.join(i['country'] + ' (' + i['error'] + ')' for i in reproduced)}" if reproduced
        else "the original code does not fail for the logged markets, so this patch is not tied to the incident")
    add("Fixes the failing markets", fixed_ok,
        f"patched code returns a finite total for {', '.join(i['country'] for i in result['fixed'])}" if fixed_ok
        else "patched code still fails or returns an invalid total for a failing market")
    return checks


class SubmitCodeFix(CodedTool):
    """Validates an LLM-written replacement for the file it just read and, if it passes, binds it to the incident."""

    def invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        """
        :param args: path, new_content (the complete corrected file), summary (one sentence).
        :param sly_data: requires a SUPPORTED correlation and a prior read_source_file call in this incident.
        """
        verified = sly_data.get(VERIFIED_KEY) or {}
        correlation = verified.get("correlation") or {}
        if correlation.get("verdict") != "SUPPORTED":
            return "Error: Refusing to propose a code fix: the root cause is not verified (correlation_check is not SUPPORTED)."
        original = sly_data.get(ORIGINAL_KEY)
        path = str(args.get("path") or "").strip()
        if not original or original["path"] != path:
            return "Error: call read_source_file for this path first; a fix must be based on the file Sentinel read."
        new_content = str(args.get("new_content") or "")
        summary = redact_text(str(args.get("summary") or "").strip())[:300] or "Code fix proposed by FixAgent"
        if not new_content.strip():
            return "Error: new_content is empty; send the complete corrected file."
        if not new_content.endswith("\n"):
            new_content += "\n"
        failing = _failing_markets(sly_data)
        checks = run_checks(path, original["content"], new_content, failing)
        all_passed = all(check["status"] == "passed" for check in checks)
        diff = list(difflib.unified_diff(original["content"].splitlines(), new_content.splitlines(),
                                         fromfile=f"a/{path}", tofile=f"b/{path}", lineterm=""))
        runbook_id = (verified.get("runbook") or {}).get("id")
        service_name = correlation.get("service_name") or "checkout-service"
        facts = {"service_name": service_name, "runbook_id": runbook_id, "fix_summary": summary, "config_file": path,
                 "diff": diff, "checks": checks, "all_passed": all_passed, "kind": "code"}
        publish(sly_data, "remediation", facts)
        ticket_notes.add_note(sly_data, f"[Code fix] {summary}\n" + "\n".join(diff[:16]))
        for check in checks:
            ticket_notes.add_note(sly_data, f"[Preflight] {check['name']}: {'PASSED' if check['status'] == 'passed' else 'FAILED'} - {check['detail']}")
        patch = {"path": path, "content": new_content, "content_sha256": _sha256(new_content),
                 "base_sha256": original["sha256"], "summary": summary}
        publish(sly_data, "code_fix", {key: patch[key] for key in ("path", "content_sha256", "base_sha256", "summary")})
        RemediationPipeline._persist_repair_binding(  # pylint: disable=protected-access
            sly_data, canonical_service_name(service_name), {"runbook_id": runbook_id}, facts, patch=patch)
        outcome = ("all preflight checks passed; a repair PR will be opened for human review" if all_passed
                   else "preflight FAILED; no PR will be opened. Send a corrected file")
        return {"all_passed": all_passed, "checks": checks, "diff": diff, "summary": f"{outcome}. No running service was changed."}

    async def async_invoke(self, args: Dict[str, Any], sly_data: Dict[str, Any]) -> Union[Dict[str, Any], str]:
        return self.invoke(args, sly_data)
