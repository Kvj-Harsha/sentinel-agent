"""Drive the live "break checkout for real, let Sentinel fix it" demo.

Every command that changes something (GitHub, the checkout app, load) refuses to run without --yes and says what it
will do first. Reads (preflight, status) are always safe.

    python scripts/live_demo.py preflight
    python scripts/live_demo.py prepare --yes    # stage the demo ONCE: buggy v2.14.0 code back in the app + a fresh deployment
    python scripts/live_demo.py reset-repo --yes # rehearsals only: put the buggy code back on GitHub main after a merged fix
    # ...then press "Inject checkout fault" in the checkout app (http://localhost:8000). That is the only trigger:
    # the app generates its own shopper traffic, Prometheus fires the alert, and Sentinel investigates by itself.
    python scripts/live_demo.py deploy --yes     # 1. record a real GitHub Deployment (the "cause" Sentinel must find)
    python scripts/live_demo.py fault  --yes     # 2. inject the checkout fault in the local demo app
    python scripts/live_demo.py load   --yes     # 3. real shopper traffic -> Prometheus alert -> Sentinel
    python scripts/live_demo.py status           # error rate + a live checkout probe, before / during / after the fix
    python scripts/live_demo.py reset  --yes     # clear the fault by hand if you need to start over
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(REPO_ROOT / ".env")

APP_URL = os.environ.get("SENTINEL_DEMO_APP_URL", "http://127.0.0.1:8000").rstrip("/")
PROMETHEUS = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
LOKI = os.environ.get("LOKI_URL", "http://127.0.0.1:3100").rstrip("/")
SENTINEL_UI = "http://127.0.0.1:8501"
OWNER = os.environ.get("SENTINEL_DEMO_GITHUB_OWNER", "")
REPO = os.environ.get("SENTINEL_DEMO_GITHUB_REPO", "")
ENVIRONMENT = os.environ.get("SENTINEL_GITHUB_DEPLOYMENT_ENVIRONMENT", "demo")
DEMO_APP_DIR = REPO_ROOT.parent / "checkout-demo-app"


def _get(url: str) -> requests.Response:
    return requests.get(url, timeout=5)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    return ok


def _need_yes(args: argparse.Namespace, action: str) -> None:
    print(f"About to: {action}")
    if not args.yes:
        raise SystemExit("Not done. Re-run with --yes to proceed.")


def error_rate() -> float | None:
    try:
        data = _get(f"{PROMETHEUS}/api/v1/query?query=" + requests.utils.quote(
            'sum(rate(checkout_requests_total{status="error"}[1m]))')).json()
        result = data["data"]["result"]
        return float(result[0]["value"][1]) if result else 0.0
    except (requests.RequestException, KeyError, ValueError, IndexError):
        return None


def probe_checkout() -> str:
    try:
        response = requests.post(f"{APP_URL}/checkout", json={"order_id": "live-probe"}, timeout=5)
        return f"HTTP {response.status_code}"
    except requests.RequestException as exc:
        return f"unreachable ({type(exc).__name__})"


def cmd_preflight(_args: argparse.Namespace) -> int:
    print("Preflight (read-only)")
    results = [
        _check("checkout app healthy", _get(f"{APP_URL}/health").ok if _reachable(APP_URL) else False, APP_URL),
        _check("Prometheus reachable", _reachable(f"{PROMETHEUS}/-/ready"), PROMETHEUS),
        _check("Loki ready", _reachable(f"{LOKI}/ready"), LOKI),
        _check("Sentinel UI up", _reachable(f"{SENTINEL_UI}/_stcore/health"), SENTINEL_UI),
        _check("SENTINEL_MODE=live in .env", os.environ.get("SENTINEL_MODE", "demo").lower() == "live",
               "set it, then recreate the sentinel containers"),
        _check("GitHub token + repo configured", bool(os.environ.get("SENTINEL_GITHUB_TOKEN") and OWNER and REPO),
               f"{OWNER}/{REPO}" if OWNER and REPO else "missing"),
        _check("Confluence configured", bool(os.environ.get("CONFLUENCE_BASE_URL") and os.environ.get("CONFLUENCE_API_TOKEN"))),
        _check("checkout demo app folder found", DEMO_APP_DIR.is_dir(), str(DEMO_APP_DIR)),
    ]
    rate = error_rate()
    print(f"  current checkout error rate: {rate if rate is not None else 'unavailable'} errors/s")
    return 0 if all(results) else 1


def _reachable(url: str) -> bool:
    try:
        return _get(url).ok
    except requests.RequestException:
        return False


def cmd_deploy(args: argparse.Namespace) -> int:
    token = os.environ.get("SENTINEL_GITHUB_TOKEN")
    if not (token and OWNER and REPO):
        raise SystemExit("Set SENTINEL_GITHUB_TOKEN, SENTINEL_DEMO_GITHUB_OWNER and SENTINEL_DEMO_GITHUB_REPO in .env.")
    _need_yes(args, f"create a GitHub Deployment on {OWNER}/{REPO} (environment '{ENVIRONMENT}') and mark it successful")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    base = f"https://api.github.com/repos/{OWNER}/{REPO}"
    branch = requests.get(base, headers=headers, timeout=10).json().get("default_branch", "main")
    created = requests.post(f"{base}/deployments", headers=headers, timeout=10, json={
        "ref": branch, "environment": ENVIRONMENT, "auto_merge": False, "required_contexts": [],
        "description": "checkout-service v2.14.0 (tax-calc-lib v3) - demo deploy",
        "transient_environment": True, "production_environment": False})
    if created.status_code == 403:
        raise SystemExit("GitHub refused: the token needs 'Deployments: Read and write' on this repo.")
    created.raise_for_status()
    deployment_id = created.json()["id"]
    status = requests.post(f"{base}/deployments/{deployment_id}/statuses", headers=headers, timeout=10,
                           json={"state": "success", "environment": ENVIRONMENT,
                                 "description": "Deployed v2.14.0 to the demo environment"})
    status.raise_for_status()
    print(f"Deployment {deployment_id} recorded as success in '{ENVIRONMENT}'. Inject the fault next (a minute or so later is fine).")
    return 0


def _buggy_pricing() -> bytes:
    return (DEMO_APP_DIR / "scenario" / "pricing_v2_14_0.py").read_bytes()


def cmd_prepare(args: argparse.Namespace) -> int:
    _need_yes(args, "put the app back on the buggy v2.14.0 pricing code and re-arm the demo (shared volume only), clear "
                    f"any running traffic, and record a fresh GitHub Deployment on {OWNER}/{REPO}")
    # Both files go into the shared volume through stdin: Docker Desktop bind mounts fail on some hosts.
    subprocess.run(["docker", "run", "--rm", "-i", "-v", "checkout-demo-settings:/config", "alpine", "sh", "-c",
                    "cat > /config/pricing.py"], input=_buggy_pricing(), check=True)
    subprocess.run(["docker", "run", "--rm", "-v", "checkout-demo-settings:/config", "alpine", "sh", "-c",
                    "printf '{\n  \"demo_faults_enabled\": true,\n  \"checkout_error_rate\": 0.0\n}\n' > /config/settings.json"
                    " && chown -R 10001:10001 /config && chmod 644 /config/settings.json /config/pricing.py"], check=True)
    print(requests.post(f"{APP_URL}/demo/recover", timeout=5).json())
    code = cmd_deploy(args)
    print("Ready. Now press 'Start incident' at", APP_URL, "- nothing else is needed.")
    return code


def cmd_reset_repo(args: argparse.Namespace) -> int:
    """Rehearsals: after a fix PR was merged, put the buggy v2.14.0 pricing back on the repository's main branch."""
    token = os.environ.get("SENTINEL_GITHUB_TOKEN")
    if not (token and OWNER and REPO):
        raise SystemExit("Set SENTINEL_GITHUB_TOKEN, SENTINEL_DEMO_GITHUB_OWNER and SENTINEL_DEMO_GITHUB_REPO in .env.")
    _need_yes(args, f"commit the buggy v2.14.0 app/pricing.py to the default branch of {OWNER}/{REPO} (demo rehearsal reset)")
    import base64  # pylint: disable=import-outside-toplevel
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    base = f"https://api.github.com/repos/{OWNER}/{REPO}"
    branch = requests.get(base, headers=headers, timeout=10).json().get("default_branch", "main")
    current = requests.get(f"{base}/contents/app/pricing.py", headers=headers, params={"ref": branch}, timeout=10).json()
    buggy = _buggy_pricing()
    if base64.b64decode(current.get("content", "")) == buggy:
        print("Repository already has the buggy v2.14.0 pricing; nothing to reset.")
        return 0
    response = requests.put(f"{base}/contents/app/pricing.py", headers=headers, timeout=10, json={
        "message": "Reset demo scenario: restore v2.14.0 pricing (rehearsal)", "branch": branch,
        "content": base64.b64encode(buggy).decode("ascii"), "sha": current.get("sha")})
    response.raise_for_status()
    print("Repository reset to the buggy v2.14.0 pricing on", branch)
    return 0


def cmd_fault(args: argparse.Namespace) -> int:
    _need_yes(args, f"POST {APP_URL}/demo/fault (checkout calls start failing on the LOCAL demo app)")
    print(requests.post(f"{APP_URL}/demo/fault", timeout=5).json())
    return 0


def cmd_load(args: argparse.Namespace) -> int:
    _need_yes(args, "run 2 minutes of k6 shopper traffic against the local checkout app (docker compose --profile load)")
    # The script is streamed over stdin so Docker Desktop never has to bind-mount it (that mount fails on some hosts).
    script = (DEMO_APP_DIR / "load" / "checkout.js").read_bytes()
    return subprocess.run(
        ["docker", "run", "--rm", "-i", "--add-host", "host.docker.internal:host-gateway", "grafana/k6:0.53.0",
         "run", "--vus", "100", "--duration", "2m", "-"],
        input=script, check=False,
    ).returncode


def cmd_reset(args: argparse.Namespace) -> int:
    _need_yes(args, f"POST {APP_URL}/demo/recover (clear the fault switch by hand)")
    print(requests.post(f"{APP_URL}/demo/recover", timeout=5).json())
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    rate = error_rate()
    print(f"checkout probe: {probe_checkout()}")
    print(f"error rate (1m): {'unavailable' if rate is None else f'{rate:.2f} errors/s'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["preflight", "prepare", "reset-repo", "deploy", "fault", "load", "status", "reset"])
    parser.add_argument("--yes", action="store_true", help="confirm a command that changes something")
    args = parser.parse_args()
    return {"preflight": cmd_preflight, "prepare": cmd_prepare, "reset-repo": cmd_reset_repo, "deploy": cmd_deploy, "fault": cmd_fault, "load": cmd_load,
            "status": cmd_status, "reset": cmd_reset}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
