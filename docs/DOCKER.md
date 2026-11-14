# Running the full stack with Docker Compose

This is the optional live-mode setup (Prometheus, Alertmanager, Loki, Alloy and the Sentinel containers). For the
fixture-backed demo you only need the quickstart in the [README](../README.md).

## Canonical entry point (PowerShell)

Use the wrapper to combine alerting, app, Loki and Alloy, with the local-safe override applied last:

```powershell
.\scripts\compose.ps1 config --quiet
.\scripts\compose.ps1 config --services
.\scripts\compose.ps1 preflight
.\scripts\compose.ps1 ps -a
```

It resolves paths relative to the script and preserves the `sentinel-agent` project and existing volumes.
The app Compose file is an override and cannot start the complete stack alone. The wrapper forwards Compose
arguments and returns Docker's exit code. It requires Docker Compose, the repository `.env`, and the existing
external volume prerequisites described in [`extras/LOCAL_ALERTING_DEMO.md`](../extras/LOCAL_ALERTING_DEMO.md).

The local override explicitly disables ServiceNow, removes its credentials, clears the GitHub repair token,
disables automatic PR creation and selects SQLite for both Sentinel processes. This overrides `.env` values.
GitHub deployment evidence uses its separate read token; Confluence reads and LLM calls retain their existing
configuration. LLM calls can incur API charges. GitHub PR/merge actions are unavailable in this local configuration;
the source-repair reader that uses the same GitHub repair client is also unavailable in live mode.

The webhook and Alertmanager read the same existing external secret volume, mounted read-only. The webhook uses
`/run/secrets/sentinel_webhook_token`; its separate environment token is cleared in this profile.

`preflight` requires the repository `.venv`, Docker access and the existing `sentinel-agent:local` image. It validates
required configuration without printing credentials, then checks the secret using a temporary non-root,
network-disabled container and a read-only mount. It never pulls an image or creates a missing secret volume.
Missing/invalid configuration, inaccessible Docker, an unreadable/invalid secret or a probe timeout returns a nonzero
exit status with an actionable message. This checks configuration presence, not live LLM/API authentication or readiness.

The wrapper does not change the host `.env` or native Python startup behavior. Directly combining only the original
four files bypasses the local override. With the documented external-volume/config prerequisites satisfied, use:

```powershell
docker compose -f ..\checkout-demo-app\docker-compose.yml build checkout-demo
.\scripts\compose.ps1 build sentinel prometheus alertmanager loki alloy
.\scripts\compose.ps1 preflight
docker compose -f ..\checkout-demo-app\docker-compose.yml up -d --no-build --wait --wait-timeout 60 checkout-demo
.\scripts\compose.ps1 up -d --no-build --wait --wait-timeout 120
.\scripts\compose.ps1 ps -a
curl.exe --fail http://localhost:8501/healthz
```

The webhook shares the image built by the `sentinel` target. Open **http://localhost:8501/**. Run a single Sentinel
web server: a native `python apps/sentinel/web.py` process can occupy port 8501 and prevent Docker startup. Identify
the owning process before stopping it; preserve active investigations and avoid stopping unrelated applications.

## Health and telemetry checks

Build/start and endpoint checks passed in checkpoint 1.7. Loki and Alloy have no Docker healthcheck; check
`http://localhost:3100/ready` and `http://localhost:12345/-/ready` directly. Loki can briefly return 503 during startup.
Fresh telemetry passed in checkpoint 1.8. To repeat the check from the host, run:

```powershell
.\.venv\Scripts\python.exe scripts/check_telemetry.py
```

It sends six healthy US demo orders with a unique log marker, then requires a fresh successful-counter scrape
and six matching current Loki lines. It does not inject a fault or call the LLM/ticket integrations.
Controlled restart/persistence checks passed in checkpoint 1.9. Full incident/browser E2E remains a separate gate in
[PLAN.md](PLAN.md) and [the checkpoint log](CHECKPOINT_LOG.md).

## Auditing the build context

Before rebuilding, inspect the real Docker-filtered Sentinel context without copying it into an image:

```powershell
docker build --file extras/ops/context-audit.Dockerfile --network none --no-cache --progress plain --output type=cacheonly .
```

This requires the existing local Sentinel image as a Python runtime and BuildKit. The read-only audit reports paths
and categories only, checks excluded local artifacts and recognized credential patterns, and exports no application
image. It is not a complete secret scan or an audit of older image layers/Git history.
