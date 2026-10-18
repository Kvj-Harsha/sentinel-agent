"""Build-context audit detects local artifacts and suppresses secret values."""

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("sentinel_context_audit", SCRIPTS / "audit_build_context.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


@pytest.mark.parametrize("path", [
    ".env", "nested/.env", "nested/.env.local", "nested/.envrc", "nested/.secrets/token",
    "nested/venv/x", "nested/__pycache__/x.pyc", "logs/app.log", "private/signing.pem",
    "private/signing.key", "deep/store.db", "deep/store.db-wal", "deep/store.sqlite3",
    "deep/.reset-backups/state", "deep/.codex/state.json", "deep/.agents/state.json",
    "eval/_thinking/trace.json", "eval/_reports/report.json",
])
def test_private_paths_are_detected(path):
    assert audit.private_path(Path(path))


@pytest.mark.parametrize("path", [".env.example", "nested/.env.example", "apps/sentinel/web/app.js", "registries/sentinel.hocon", "tests/test_tools.py"])
def test_application_and_example_paths_are_retained(path):
    assert not audit.private_path(Path(path))


def test_context_findings_suppress_token_content(tmp_path):
    # Build the fixture at runtime so the checked-in test is not itself a credential.
    token = "sk-" + "x" * 30
    (tmp_path / "config.py").write_text('value = "' + token + '"', encoding="utf-8")
    count, findings = audit.audit(tmp_path)
    assert count == 1
    assert findings and "provider/service token" in str(findings)
    assert token not in str(findings)


def test_private_file_is_not_read(tmp_path, monkeypatch):
    file = tmp_path / ".env"
    file.write_text("private content", encoding="utf-8")
    monkeypatch.setattr(Path, "read_text", lambda *a, **k: pytest.fail("Private file content was read"))
    assert audit.audit(tmp_path)[1] == [(".env", "private/local artifact included")]
