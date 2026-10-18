# Sentinel

**An incident-triage agent network that refuses to guess.** An alert fires; a team of neuro-san agents gathers logs,
metrics, deploy history and runbook evidence; a *deterministic* check decides whether the evidence supports a root
cause; Sentinel opens a ticket and proposes a bounded fix that a **human** reviews and merges.

> LLMs route and explain. Coded tools decide. Every piece of evidence is labelled `fixture`, `live` or `unavailable`.

Built on Cognizant's [neuro-san](https://github.com/cognizant-ai-lab/neuro-san) framework (agents declared in HOCON).

## 60-second demo

```sh
python -m venv .venv && .venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
# Configure OPENAI_API_KEY in .env using .env.example; preserve any existing .env.
python apps/sentinel/web.py
```

Open http://localhost:8501, keep the `checkout-500` sample selected and press **Investigate alert** (about a minute).
You get a ticket, a `SUPPORTED` verdict re-derived from source timestamps (deploy v2.14.0 landed 4.0 min before the
errors), the runbook, and a locked-until-supported **Prepare repair PR** panel. Pick `inventory-timeout` instead to see
Sentinel say `NOT_SUPPORTED` rather than invent a cause.

By default Sentinel runs in **demo mode**: bundled evidence is replayed from `coded_tools/sentinel/data/` and labeled
`fixture`. Agent reasoning still uses the configured LLM API and may incur charges. Native Python startup loads `.env`;
use the local-safe Docker workflow below when external ticket/repair writes must be disabled.

## Live mode (optional)

Set `SENTINEL_MODE=live` in `.env` to switch the evidence tools to real sources:

| Evidence | Demo mode (default) | Live mode |
|---|---|---|
| Metrics | fixture | Prometheus HTTP API |
| Logs | fixture | Grafana Loki |
| Deploys | fixture | GitHub Deployments + statuses |
| Runbooks | fixture | Confluence (read-only) |
| Tickets | local SQLite | local SQLite, mirrored to ServiceNow if configured |

The Prometheus/Alertmanager/Loki stack and containers live in [extras/](extras/); see
[extras/LOCAL_ALERTING_DEMO.md](extras/LOCAL_ALERTING_DEMO.md) and [extras/OPERATIONS.md](extras/OPERATIONS.md).
The human-reviewed GitHub repair PR needs `SENTINEL_GITHUB_TOKEN` and a demo repo (see `.env.example`); the *Prepare repair
PR* button creates a real issue and PR in that repo.

### Canonical Docker Compose entry point (PowerShell)

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
external volume prerequisites described in `extras/LOCAL_ALERTING_DEMO.md`.

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

Build/start and endpoint checks passed in checkpoint 1.7. Loki and Alloy have no Docker healthcheck; check
`http://localhost:3100/ready` and `http://localhost:12345/-/ready` directly. Loki can briefly return 503 during startup.
Fresh telemetry passed in checkpoint 1.8. To repeat the check from the host, run:

```powershell
.\.venv\Scripts\python.exe scripts/check_telemetry.py
```

It sends six healthy US demo orders with a unique log marker, then requires a fresh successful-counter scrape
and six matching current Loki lines. It does not inject a fault or call the LLM/ticket integrations.
Controlled restart/persistence checks passed in checkpoint 1.9. Full incident/browser E2E remains a separate gate in
[plan.md](plan.md) and [the checkpoint log](docs/CHECKPOINT_LOG.md).

Before rebuilding, inspect the real Docker-filtered Sentinel context without copying it into an image:

```powershell
docker build --file extras/ops/context-audit.Dockerfile --network none --no-cache --progress plain --output type=cacheonly .
```

This requires the existing local Sentinel image as a Python runtime and BuildKit. The read-only audit reports paths
and categories only, checks excluded local artifacts and recognized credential patterns, and exports no application
image. It is not a complete secret scan or an audit of older image layers/Git history.

## How the network works

```
IncidentCommander (front man, PII guardrail)
 ├─ Responder ........ open_ticket · recall_incidents · finalize_incident · update_ticket
 ├─ LogsAgent ........ loki_logs
 ├─ MetricsAgent ..... prometheus_metrics
 ├─ DeployHistoryAgent github_deployments · deploy_lookup
 ├─ RunbookAgent ..... runbook_search
 └─ Scribe ........... correlation_check   (deterministic evaluation loop)
```

- **[registries/sentinel.hocon](registries/sentinel.hocon)** is the single source of truth for agents, tool routing,
  middleware and `sly_data` exposure. Python only starts the network.
- **[coded_tools/sentinel/](coded_tools/sentinel/)** holds the deterministic tools. Tools publish evidence into
  `sly_data`; `correlation_check` re-derives the incident and deploy timestamps from that evidence itself instead of
  trusting timestamps retold by an LLM.
- **`finalize_incident`** is the close-out tool: it records incident memory and, only for a `SUPPORTED` verdict, prepares
  the allow-listed config fix with local preflight checks. The ticket stays *In Progress* until a human approves the PR.
- Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Repository layout

```
registries/        HOCON agent network (manifest, sentinel, llm_config, AAOSA includes)
coded_tools/sentinel/   deterministic tools, evidence schemas, stores, data/ fixtures
apps/sentinel/     web.py + web/ (responsive HTML UI), run.py (neuro-san bootstrap), alerts.py, alertmanager_receiver.py
tests/             offline pytest suite (no LLM, no network)
eval/              opt-in live LLM scenarios (uses API credits)
extras/            optional ops: Docker, Prometheus/Alertmanager/Loki/Alloy, webhook service, runbooks, old plan
docs/              ARCHITECTURE.md, diagrams
```

## Tests

```sh
pytest                      # unit/integration tests; observed results are in docs/CHECKPOINT_LOG.md
python eval/run_eval.py     # live scenarios (~10 min, uses API credits)
python -m neuro_san.client.hocon_validator_cli registries/sentinel.hocon
```

## Safety model

Fixed, allow-listed repair scope; PR head SHA and checks re-verified before merge; no auto-merge; PII redaction on the
front man; secrets only in the git-ignored `.env`. See [extras/SECURITY_AND_RETENTION.md](extras/SECURITY_AND_RETENTION.md).

Ticket mutations require the private incident session to own the ticket. Fingerprint retries reuse its canonical
session. The HTML repair API requires `ticket_number` for PR creation; merge and recovery requests also require
the reviewed integer `pr_number` and full `sha` from `/api/state`, with `approved: true` for merge. Stale or missing
bindings are rejected; API callers must send these fields. Recovery after merge stays pinned to the reviewed ticket
even if another incident becomes active. These checks do not provide user authentication or distributed job locking.

## License

Apache-2.0. `aaosa_basic.hocon` and `expertise_scoping_instructions.hocon` are unmodified copies from
[neuro-san-studio](https://github.com/cognizant-ai-lab/neuro-san-studio). Fixtures are synthetic.
