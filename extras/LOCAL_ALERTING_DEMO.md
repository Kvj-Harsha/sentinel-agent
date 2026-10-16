# Local Alertmanager End-to-End Demo

This runbook exercises the isolated checkout app → Prometheus → Alertmanager → Sentinel webhook path. It uses the
configured model provider to investigate one alert and stores a local incident ticket. It does not require a GitHub
repair token or ServiceNow account. The checkout app is a separate local project at `../checkout-demo-app`.

## Prerequisites

- Docker Desktop is running.
- The checkout demo app has `demo_faults_enabled: true` in `../checkout-demo-app/app/settings.json`.
- Sentinel's `.env` has an LLM API key and a `SENTINEL_WEBHOOK_TOKEN` of at least 24 characters.
- `.secrets/sentinel_webhook_token` contains the same webhook token. Keep both values private.
- No unrelated application is using ports 8000, 8090, 8501, 9090, or 9093.

For a local-only run, unset the integration variables in the shell that starts Sentinel. This prevents configured
ServiceNow and GitHub repair credentials in `.env` from being used by the demo process:

```powershell
$env:SERVICENOW_INSTANCE = ''
$env:SERVICENOW_USER = ''
$env:SERVICENOW_PASSWORD = ''
$env:SENTINEL_GITHUB_TOKEN = ''
$env:SENTINEL_DEMO_GITHUB_OWNER = ''
$env:SENTINEL_DEMO_GITHUB_REPO = ''
```

## Start the services

The Alertmanager config is copied into a local image so Docker Desktop does not need to bind-mount it. Its token is
stored in a Docker named volume. Seed that volume once from PowerShell in the Sentinel repository; this copies the
token without printing it:

```powershell
docker volume create sentinel-agent_alertmanager-secrets
docker create --name sentinel-alertmanager-secret-init --mount "type=volume,source=sentinel-agent_alertmanager-secrets,target=/run/secrets" prom/alertmanager:v0.28.1
docker cp .secrets/sentinel_webhook_token sentinel-alertmanager-secret-init:/run/secrets/sentinel_webhook_token
docker rm sentinel-alertmanager-secret-init
```

Keep the named volume when restarting the stack. Then, in PowerShell from the Sentinel repository:

```powershell
docker compose -f docker-compose.alerting.yml up -d
streamlit run frontend/run.py --server.port 8501
```

In a second terminal, start the checkout app:

```powershell
Set-Location ..\checkout-demo-app
docker compose up -d --build
```

From the Sentinel repository, start the local log store and collector:

```powershell
docker compose -f docker-compose.loki.yml up -d
docker compose -f docker-compose.alloy.yml up -d
```

Grafana Alloy reads Docker logs through the local Docker socket and filters discovery to the `checkout-demo`
Compose service before forwarding them to Loki. This collector is for local development only; mounting the Docker
socket grants broad control over the Docker daemon. Loki is available at `http://localhost:3100/ready` and Alloy's
local status UI at `http://localhost:12345`. Start Alloy only after reviewing and approving that Docker access.

Wait for these pages to respond:

- Checkout app: <http://localhost:8000/health>
- Prometheus: <http://localhost:9090/targets>
- Loki: <http://localhost:3100/ready>
- Grafana Alloy: <http://localhost:12345>
- Alertmanager: <http://localhost:9093>
- Sentinel: <http://localhost:8501>
- Sentinel receiver health: <http://localhost:8090/healthz>

Prometheus' `checkout-demo` target must be `UP`; Sentinel's Prometheus · Alertmanager panel must say the receiver
is listening. The health endpoint returns status only and never includes the bearer token.

If Docker Desktop reports a `/run/desktop/mnt/host/c` bind-mount error, check Docker Desktop's access to the
repository path before recreating existing monitoring containers. If only the k6 Compose bind mount fails, run
the same checked-in script through stdin from the checkout app folder:

```powershell
Get-Content -Raw .\load\checkout.js | docker run --rm -i --add-host=host.docker.internal:host-gateway grafana/k6:0.53.0 run --vus 100 --duration 2m -
```

## Cause and observe one alert

Inject the fault through the checkout app's local UI or from PowerShell. Run k6 from the checkout app directory
because its Compose file owns the load profile:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/demo/fault
Set-Location ..\checkout-demo-app
docker compose --profile load run --rm k6
```

The included k6 profile runs for two minutes. Prometheus scrapes every 15 seconds and the alert rule requires the
measured 5xx rate to exceed 0.5 requests/second for 15 seconds, so allow up to about one minute for the alert to
fire and Alertmanager to deliver it. Sentinel should show one firing alert, an investigation job that completes,
one local incident ticket, and verified evidence. The receiver accepts firing alerts only. Alertmanager is
configured with `send_resolved: false`; if a resolved event is sent manually, Sentinel reports it as ignored.

## Verify webhook safeguards

- **Authentication:** POST to `/alerts/alertmanager` without an `Authorization: Bearer ...` header; expect HTTP 401.
- **Duplicate delivery:** replay the same firing alert with its original fingerprint; expect `status: duplicate` and
  confirm the UI still shows one job/ticket for that fingerprint.
- **Resolved policy:** send an alert with `status: resolved`; expect it under `ignored`, with no investigation job.
- **Payload bounds:** a body over 1 MiB returns HTTP 413; more than five alerts in one request returns HTTP 400.
- **Error visibility:** malformed JSON returns HTTP 400 with a concise message; the UI shows failed jobs without
  exposing the bearer token or original raw request body.

Do not paste the bearer token into screenshots, shell history, or chat. Keep verification payloads synthetic and
omit customer data.

## Recover and stop

Clear the fault after the investigation. The k6 profile exits automatically after its two-minute run:

```powershell
Invoke-RestMethod -Method Post -Uri http://localhost:8000/demo/recover
```

For containers started by this run, stop the checkout app from its repository and the monitoring stack from
Sentinel's repository:

```powershell
# From ../checkout-demo-app
docker compose down

# From the Sentinel repository
docker compose -f docker-compose.alerting.yml down

# From the Sentinel repository
docker compose -f docker-compose.loki.yml down
docker compose -f docker-compose.alloy.yml down
```

Stop Streamlit with `Ctrl+C`. If these services were already running before the demo, leave them running and do not
claim teardown; only stop containers you started for this run.

If you restored Alertmanager manually after a Docker Desktop bind-mount failure and `docker compose ps` does not
list it, stop that restored container explicitly before tearing down the project:

```powershell
docker stop sentinel-agent-alertmanager-1
docker rm sentinel-agent-alertmanager-1
```
