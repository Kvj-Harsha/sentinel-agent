"""Configuration and secret preflight checks must fail safely without disclosing values."""

import copy
import importlib.util
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location("sentinel_preflight", Path(__file__).resolve().parents[1] / "scripts/preflight.py")
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


@pytest.fixture
def config():
    env = {
        "OPENAI_API_KEY": "fixture-llm-key", "SENTINEL_MODE": "live",
        "SENTINEL_STORAGE_BACKEND": "sqlite", "SENTINEL_DB_PATH": "/var/lib/sentinel/sentinel.db",
        "SERVICENOW_ENABLED": "false", "SENTINEL_AUTO_PR": "false",
        "SENTINEL_GITHUB_TOKEN": "", "SERVICENOW_INSTANCE": "", "SERVICENOW_USER": "", "SERVICENOW_PASSWORD": "",
    }
    mount = {"type": "volume", "source": "secret", "target": "/run/secrets", "read_only": True}
    return {"services": {
        "sentinel": {"environment": env},
        "sentinel-webhook": {"environment": {**env, "SENTINEL_WEBHOOK_TOKEN_FILE": preflight.SECRET_PATH}, "volumes": [copy.deepcopy(mount)]},
        "alertmanager": {"volumes": [copy.deepcopy(mount)]},
    }, "volumes": {"secret": {"external": True, "name": "fixture-secret-volume"}}}


def test_valid_config(config):
    assert preflight.validate_config(config) == ([], "fixture-secret-volume")


@pytest.mark.parametrize("key,value,message", [
    ("OPENAI_API_KEY", "sk-...", "OPENAI_API_KEY"),
    ("SENTINEL_MODE", "typo", "SENTINEL_MODE"),
    ("SENTINEL_STORAGE_BACKEND", "json", "SQLite"),
    ("SERVICENOW_ENABLED", "true", "write-disable"),
    ("SERVICENOW_PASSWORD", "sensitive-fixture-must-not-appear", "credentials"),
    ("SENTINEL_WEBHOOK_PORT", "1234", "8090"),
    ("SENTINEL_WEBHOOK_TOKEN", "sensitive-fixture-must-not-appear", "mounted token"),
])
def test_invalid_configuration_has_actionable_redacted_errors(config, key, value, message):
    config["services"]["sentinel-webhook"]["environment"][key] = value
    errors, _ = preflight.validate_config(config)
    assert any(message in error for error in errors)
    assert "sensitive-fixture-must-not-appear" not in str(errors)


@pytest.mark.parametrize("mutation", ["missing", "writable", "different", "not-external"])
def test_invalid_mount_rejected(config, mutation):
    mount = config["services"]["sentinel-webhook"]["volumes"][0]
    if mutation == "missing":
        config["services"]["sentinel-webhook"]["volumes"] = []
    elif mutation == "writable":
        mount["read_only"] = False
    elif mutation == "different":
        mount["source"] = "another-secret"
    else:
        config["volumes"]["secret"]["external"] = False
    assert preflight.validate_config(config)[0]


@pytest.mark.parametrize("token,code", [
    (b"a" * 32 + b"\n", 0), (b"short", 4), (b"a" * 25 + b"\ninside", 4),
    (b"<random-secret-at-least-24-characters>", 4), (b"a" * 4097, 4), (b"\xff" * 32, 4),
    (None, 3),
])
def test_actual_probe_validates_token_without_output(tmp_path, token, code):
    path = tmp_path / "secret"
    if token is not None:
        path.write_bytes(token)
    program = preflight.PROBE.replace("'/run/secrets/sentinel_webhook_token'", repr(str(path)))
    result = subprocess.run([sys.executable, "-I", "-c", program], capture_output=True, timeout=5)
    assert result.returncode == code
    assert result.stdout == result.stderr == b""


@pytest.mark.parametrize("codes,message,count", [
    ([1], "volume unavailable", 1), ([0, 1], "image unavailable", 2),
    ([0, 0, 3], "unreadable", 3), ([0, 0, 4], "invalid", 3),
    ([0, 0, 125], "container failed", 3), ([0, 0, 0], None, 3),
])
def test_docker_probe_failure_and_success(codes, message, count):
    commands = []

    def runner(argv, **kwargs):
        commands.append(argv)
        return SimpleNamespace(returncode=codes[len(commands) - 1])

    result = preflight.probe_secret("fixture-secret-volume", runner)
    assert len(commands) == count
    if message:
        assert message in result
    else:
        assert result is None
        cmd = commands[-1]
        assert cmd[cmd.index("--network") + 1] == "none"
        assert cmd[cmd.index("--pull") + 1] == "never"
        assert cmd[cmd.index("--user") + 1] == "10001:10001"
        assert "--read-only" in cmd and "--rm" in cmd
        assert "readonly" in cmd[cmd.index("--mount") + 1]


def test_probe_timeout_is_safe():
    def runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("docker", 25)
    assert "timed out" in preflight.probe_secret("fixture-secret-volume", runner)
