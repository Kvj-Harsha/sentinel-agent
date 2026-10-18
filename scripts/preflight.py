"""Validate resolved Compose configuration and probe the shared token without printing secrets.

Use scripts/compose.ps1 preflight. Resolved Compose JSON is consumed from stdin,
never written to disk. No service is started; the probe uses a temporary isolated
container against an existing volume and existing local image only.
"""

import json
from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATH = "/run/secrets/sentinel_webhook_token"
PROBE = """
from pathlib import Path
import sys
try:
    with Path('/run/secrets/sentinel_webhook_token').open('rb') as f:
        raw = f.read(4097)
    token = raw.strip()
    valid = (len(raw) <= 4096 and len(token) >= 24 and token.isascii()
             and not any(chr(c).isspace() for c in token)
             and b'<' not in token and b'>' not in token)
except OSError:
    sys.exit(3)
sys.exit(0 if valid else 4)
"""


def present(value):
    """Reject empty or example credential values without including them in errors."""
    text = str(value or "").strip()
    return bool(text and "..." not in text and "<" not in text and ">" not in text)


def validate_config(config):
    """Return static, value-free failures and the resolved existing secret volume name."""
    errors = []
    services = config.get("services", {})
    for name in ("sentinel", "sentinel-webhook", "alertmanager"):
        if name not in services:
            errors.append(f"Missing service {name}; use scripts/compose.ps1 preflight.")
    if errors:
        return errors, None
    for name in ("sentinel", "sentinel-webhook"):
        env = services[name].get("environment", {})
        if not present(env.get("OPENAI_API_KEY")):
            errors.append(f"{name}: set a real OPENAI_API_KEY in the repository .env.")
        if str(env.get("SENTINEL_MODE", "demo")).strip().lower() not in {"demo", "live"}:
            errors.append(f"{name}: SENTINEL_MODE must be demo or live.")
        if env.get("SENTINEL_STORAGE_BACKEND") != "sqlite" or env.get("SENTINEL_DB_PATH") != "/var/lib/sentinel/sentinel.db":
            errors.append(f"{name}: use the canonical SQLite backend and container database path.")
        if env.get("SERVICENOW_ENABLED") != "false" or env.get("SENTINEL_AUTO_PR") != "false":
            errors.append(f"{name}: local write-disable flags are missing; use the canonical wrapper.")
        if any(env.get(key) for key in ("SENTINEL_GITHUB_TOKEN", "SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD")):
            errors.append(f"{name}: remote write credentials must be empty in the local profile.")
    if str(services["sentinel"].get("environment", {}).get("SENTINEL_UI_PORT", "8501")) != "8501":
        errors.append("sentinel: SENTINEL_UI_PORT must match the configured container port 8501.")
    webhook_env = services["sentinel-webhook"].get("environment", {})
    if str(webhook_env.get("SENTINEL_WEBHOOK_PORT", "8090")) != "8090":
        errors.append("sentinel-webhook: SENTINEL_WEBHOOK_PORT must match Alertmanager target port 8090.")
    if webhook_env.get("SENTINEL_WEBHOOK_TOKEN_FILE") != SECRET_PATH or webhook_env.get("SENTINEL_WEBHOOK_TOKEN"):
        errors.append("sentinel-webhook: use only the shared mounted token file.")
    source = None
    for name in ("alertmanager", "sentinel-webhook"):
        mounts = [m for m in services[name].get("volumes", []) if m.get("target") == "/run/secrets"]
        if len(mounts) != 1 or mounts[0].get("type") != "volume" or not mounts[0].get("read_only"):
            errors.append(f"{name}: mount the existing shared secret volume read-only at /run/secrets.")
        elif source is not None and source != mounts[0].get("source"):
            errors.append("Alertmanager and webhook must use the same secret volume.")
        else:
            source = mounts[0].get("source")
    volume = config.get("volumes", {}).get(source, {})
    if not volume.get("external") or not volume.get("name"):
        errors.append("The webhook secret volume must be external and explicitly named.")
    # Match the checked-in Alertmanager image configuration rather than assuming its path.
    am = yaml.safe_load((ROOT / "extras/ops/alertmanager/container-alertmanager.yml").read_text(encoding="utf-8"))
    hooks = [h for r in am.get("receivers", []) for h in r.get("webhook_configs", [])]
    if not hooks or any(h.get("http_config", {}).get("authorization", {}).get("credentials_file") != SECRET_PATH for h in hooks):
        errors.append("Alertmanager credentials_file must match the webhook secret file.")
    return errors, volume.get("name")


def probe_secret(volume, runner=subprocess.run):
    """Check existence before mounting: never auto-create a missing named volume."""
    common = {"capture_output": True, "text": True, "timeout": 25, "check": False}
    try:
        if runner(["docker", "volume", "inspect", volume], **common).returncode:
            return "Secret volume unavailable: check Docker access and provision the existing external volume."
        if runner(["docker", "image", "inspect", "sentinel-agent:local"], **common).returncode:
            return "Local sentinel-agent:local image unavailable: build it before the secret probe; no image was pulled."
        result = runner([
            "docker", "run", "--rm", "--pull", "never", "--network", "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "10001:10001", "--mount", f"type=volume,src={volume},dst=/run/secrets,readonly",
            "--entrypoint", "python", "sentinel-agent:local", "-c", PROBE,
        ], **common)
    except (OSError, subprocess.TimeoutExpired):
        return "Docker secret probe unavailable or timed out; verify Docker access and retry."
    if result.returncode == 3:
        return "Secret file missing or unreadable by UID 10001: provision sentinel_webhook_token with read permission for the webhook user."
    if result.returncode == 4:
        return "Secret invalid: use a non-placeholder, single ASCII token of at least 24 characters (file limit 4096 bytes)."
    if result.returncode:
        return "Secret probe container failed; inspect Docker availability and local image compatibility."
    return None


def main():
    try:
        config = json.load(sys.stdin)
        if not isinstance(config, dict):
            raise ValueError
        errors, volume = validate_config(config)
    except (ValueError, TypeError, KeyError, OSError, yaml.YAMLError):
        print("FAIL: Cannot validate resolved configuration; run scripts/compose.ps1 config --quiet first.")
        return 1
    if not errors:
        failure = probe_secret(volume)
        if failure:
            errors.append(failure)
    for error in errors:
        print(f"FAIL: {error}")
    if errors:
        return 1
    print("PASS: required LLM configuration, local write guards, SQLite path and configured ports.")
    print("PASS: Alertmanager and webhook share a readable, valid mounted token; value withheld.")
    print("Configuration presence only: live API authentication, balances, model availability and service readiness are not checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
