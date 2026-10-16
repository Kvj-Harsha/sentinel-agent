"""The LLM writes the patch; these checks decide whether it may become a pull request."""

import pytest

from coded_tools.sentinel import code_fix
from coded_tools.sentinel.code_fix import ReadSourceFile
from coded_tools.sentinel.code_fix import SubmitCodeFix
from coded_tools.sentinel.code_fix import run_checks

ORIGINAL = code_fix.SCENARIO_FILE.read_text(encoding="utf-8")
GOOD = ORIGINAL.replace(
    "    return TAX_RATES.get(country)",
    "    return TAX_RATES.get(country, DEFAULT_TAX_RATE)",
).replace('TAX_RATES = {', 'DEFAULT_TAX_RATE = 0.10\nTAX_RATES = {')


def _status(checks):
    return {check["name"]: check["status"] for check in checks}


def test_a_correct_default_rate_patch_passes_every_check():
    checks = run_checks("app/pricing.py", ORIGINAL, GOOD, ["IN", "BR"])
    assert all(check["status"] == "passed" for check in checks), checks
    assert "Reproduces the incident on the original code" in _status(checks)


@pytest.mark.parametrize("name,candidate,failed_check", [
    ("unsafe import", "import os\n" + GOOD, "Syntax and safety (AST)"),
    ("dynamic exec", GOOD + "\nexec('x=1')\n", "Syntax and safety (AST)"),
    ("syntax error", "def order_total(:\n", "Syntax and safety (AST)"),
    ("api removed", GOOD.replace("def tax_rate(country):", "def rate_for(country):").replace("tax_rate(country)\n", "rate_for(country)\n"), "Public API preserved"),
    ("changes a supported market", GOOD.replace('"US": 0.07', '"US": 0.99'), "Behaviour preserved for supported markets"),
    ("does not fix the failure", ORIGINAL.replace("an ISO country code", "a country"), "Fixes the failing markets"),
])
def test_bad_patches_are_rejected(name, candidate, failed_check):
    checks = run_checks("app/pricing.py", ORIGINAL, candidate, ["IN", "BR"])
    assert _status(checks).get(failed_check) == "failed", (name, checks)


def test_oversized_rewrite_is_rejected():
    huge = ORIGINAL + "\n".join(f"# filler {i}" for i in range(200)) + "\n"
    assert _status(run_checks("app/pricing.py", ORIGINAL, huge, ["IN"]))["Size and diff limits"] == "failed"


def _verified(verdict="SUPPORTED"):
    return {"sentinel_verified": {"correlation": {"verdict": verdict, "service_name": "checkout-service"}},
            "sentinel_ticket_number": "INC1", "incident_session_id": "s1"}


def test_submit_requires_a_supported_verdict_and_a_prior_read(monkeypatch):
    monkeypatch.setenv("SENTINEL_MODE", "demo")
    args = {"path": "app/pricing.py", "new_content": GOOD, "summary": "Add a default tax rate"}
    assert "not verified" in SubmitCodeFix().invoke(args, _verified("NOT_SUPPORTED"))
    assert "read_source_file" in SubmitCodeFix().invoke(args, _verified())


def test_only_allow_listed_files_can_be_read(monkeypatch):
    monkeypatch.setenv("SENTINEL_MODE", "demo")
    assert "not a source file" in ReadSourceFile().invoke({"path": "app/main.py"}, {})
    assert "not a source file" in ReadSourceFile().invoke({"path": "../../.env"}, {})


def test_end_to_end_good_patch_is_published_and_bound(monkeypatch):
    monkeypatch.setenv("SENTINEL_MODE", "demo")
    sly = _verified()
    sly["sentinel_verified"]["evidence"] = [{"evidence_type": "log", "summary": "checkout failed status 503 TypeError (pricing.py 17) country=IN"}]
    read = ReadSourceFile().invoke({"path": "app/pricing.py"}, sly)
    assert read["source_status"] == "fixture" and "1:" in read["content"]

    result = SubmitCodeFix().invoke({"path": "app/pricing.py", "new_content": GOOD, "summary": "Default tax rate for unlisted markets"}, sly)

    assert result["all_passed"] is True
    facts = sly["sentinel_verified"]
    assert facts["remediation"]["all_passed"] and facts["code_fix"]["path"] == "app/pricing.py"
    assert any("IN (TypeError)" in check["detail"] for check in facts["remediation"]["checks"])


def test_code_patch_changes_branch_and_binding_marker_but_settings_repairs_do_not():
    from coded_tools.sentinel import github_pr_client as pr  # pylint: disable=import-outside-toplevel

    settings_ctx = {"session_id": "s", "service_name": "checkout-service", "verdict": "SUPPORTED", "evidence_refs": [], "all_passed": True}
    patch_ctx = {**settings_ctx, "patch": {"path": "app/pricing.py", "content": "x\n", "content_sha256": "abc"}}
    assert pr._branch_for("INC1", settings_ctx) == "sentinel/disable-demo-fault-inc1"  # unchanged for old repairs
    assert pr._branch_for("INC1", patch_ctx) == "sentinel/fix-inc1"
    assert pr._binding_marker("INC1", settings_ctx) != pr._binding_marker("INC1", patch_ctx)


def test_merged_code_replaces_the_file_atomically(tmp_path):
    from coded_tools.sentinel import github_pr_client as pr  # pylint: disable=import-outside-toplevel

    target = tmp_path / "pricing.py"
    target.write_text("old\n", encoding="utf-8")
    pr._atomic_write(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["pricing.py"]  # no temp file left behind
