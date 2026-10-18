# Sentinel checkpoint verification log

## Phase 0 — Baseline discovery closed

Date: **18 October 2026**, Asia/Calcutta. Scope: read-only inspection and planning documents. No services were started or stopped, no faults injected, and no application data or external records changed during this checkpoint.

### Repository and runtime baseline

- Sentinel: branch `hackathon-restructure`, HEAD `d232890`, with existing uncommitted changes. `git diff --stat` reported 17 tracked files, 96 insertions and 26 deletions; untracked frontend/backend files, regressions and plan are additional work. These counts precede this checkpoint's documentation edits.
- Checkout demo: `C:/Users/kvjha/Downloads/checkout-demo-app`, branch `main`, HEAD `cd50b6e`.
- Virtual environment: Python **3.14.5**, neuro-san **0.7.7**, Pydantic **2.14.1**, requests **2.34.2**, pytest **9.1.1**, python-dotenv **1.2.4**. Dockerfile uses Python 3.12; parity is a later verification requirement.
- Existing environment: mode `live`, backend `sqlite`, ServiceNow explicitly enabled. Automatic PR flag unset; current patched code defaults to opt-in. A local-safe Compose configuration remains necessary before restarting writers.

### Observed Docker state

| Container | State | Published host port / exit details |
|---|---|---|
| `checkout-demo-app-checkout-demo-1` | Running, healthy | `127.0.0.1:8000` |
| `sentinel-agent-alloy-1` | Running | `127.0.0.1:12345` |
| `sentinel-agent-loki-1` | Running | `127.0.0.1:3100` |
| `sentinel-agent-sentinel-1` | Exited | Exit 137, OOMKilled false, Docker state Error empty |
| `sentinel-agent-sentinel-webhook-1` | Exited | Exit 0, OOMKilled false |
| `sentinel-agent-prometheus-1` | Exited | Exit 255, OOMKilled false |
| `sentinel-agent-alertmanager-1` | Exited | Exit 255, OOMKilled false |

Exit codes alone do not establish a root cause. Short log tails contained Prometheus compaction/GC entries, Alertmanager successful configuration/listener startup, and Sentinel UI listener messages; none explained the current exits. Application recovery belongs to Phase 1.

Networks observed: `bridge`, `host`, `none`, `checkout-demo-app_default`, `sentinel-agent-runtime`, `sentinel-agent_default`.

Named volumes observed: `checkout-demo-settings`, `sentinel-agent_alertmanager-secrets`, `sentinel-agent_alloy-data`, `sentinel-agent_loki-data`, `sentinel-agent_prometheus-data`, `sentinel-agent_sentinel-data`; additional anonymous volumes exist and were untouched.

### Persistence and demo prerequisites

| Context | Configured path / prerequisite | Decision |
|---|---|---|
| Normal host bootstrap loading `.env` | `backend/coded_tools/sentinel/data/sentinel.db`, relative to repository root | Use the existing configured path; file exists, 114688 bytes at inspection |
| Default path when env override absent | `coded_tools/sentinel/data/sentinel.db` | Separate existing file, 69632 bytes; preserve and do not silently import/replace |
| Sentinel Docker app and webhook | `/var/lib/sentinel/sentinel.db` on `sentinel-agent_sentinel-data` | Existing intended shared store; preserve volume |
| Demo app shared settings | External volume `checkout-demo-settings`; checkout reads `/config/settings.json` | Validate existing contents/readiness during Phase 1 |
| Fault trigger | `POST /demo/fault` | Requires current settings `demo_faults_enabled=true`; otherwise 409. Source verified, current settings not probed in this checkpoint |
| Fault recovery | `POST /demo/recover` | Clears fault switch and stops synthetic traffic; not invoked here |

No decision to consolidate host and container databases is needed for baseline closure. Any future migration requires separate planning and preservation of existing state.

### Commands executed and results

| Command / inspection | Observed result |
|---|---|
| `git status --short`, `git rev-parse --short HEAD`, `git branch --show-current`, `git diff --stat` | Revision and dirty working tree captured above |
| `.\.venv\Scripts\python.exe --version` | Python 3.14.5 |
| Python `importlib.metadata.version` for five packages | Versions captured above; no installations |
| Python `dotenv_values` reading only selected nonsecret flags/path and filesystem metadata | Host path, live mode, enabled ServiceNow and two DB files confirmed; credentials not printed |
| `docker ps -a --format '{{.Names}}|{{.Status}}|{{.Ports}}'` | Seven containers captured above |
| `docker network ls --format '{{.Name}}'`, `docker volume ls --format '{{.Name}}'` | Networks/volumes captured above |
| `docker inspect --format '{{json .State}}'` for four stopped services | Selected status, exit code, OOM and error fields recorded |
| `docker logs --tail 6` for Sentinel, Prometheus and Alertmanager, passed through `redact_text` before display | Sanitized tails inspected; no failure cause established |
| `git -C C:\Users\kvjha\Downloads\checkout-demo-app rev-parse --short HEAD` and `branch --show-current` | `cd50b6e`, `main` |
| Read demo Compose/fault/recovery source and SQLite constructor | Prerequisites/path selection confirmed |

Docker and sibling Git initially failed under the filesystem sandbox. The same read-only inspections succeeded after approved escalation. The earlier approval usage-limit failure was not reproduced for these inspections; it does not establish that the unexecuted test rerun has passed.

### Tests and closure evidence

No tests were executed during this baseline checkpoint. Historical evidence remains: baseline suite 59 passed; new pre-fix regressions 15 failed / 1 passed; post-fix rerun pending. Container running status is not a readiness/data-flow test.

**Closure:** Baseline, current failures, persistence boundaries and demo prerequisites are documented without changing user data. All Phase 0 discovery items are satisfied. Next checkpoint: **1.1 — provide and validate a canonical Compose entry point**, followed by **1.2 — explicit local-safe configuration** before starting incident writers. No new scope decision is required at this point.

## Checkpoint 1.1 — Canonical Compose entry point closed

Date: **18 October 2026**, Asia/Calcutta.

**Change:** Added `scripts/compose.ps1` and README instructions. The wrapper combines alerting, app, Loki and Alloy Compose files in the existing order, explicitly selects the `extras` project directory and `sentinel-agent` project name, forwards arguments, and propagates Docker's exit code. Paths resolve relative to the script; no files were merged or services rewritten.

### Executed checks

| Check | Command / method | Result |
|---|---|---|
| Compose syntax | `.\scripts\compose.ps1 config --quiet` | Passed, exit 0 |
| Service inventory | `.\scripts\compose.ps1 config --services` | Passed: sentinel, sentinel-webhook, alertmanager, prometheus, alloy, loki |
| Full configuration equivalence | Wrapper `config --format json` piped directly into Python; compared in memory against the original four `-f` arguments using parsed JSON | Passed; resolved environment values withheld |
| Persistent resource identity | Assertions on parsed project and volume names | Passed: existing project, Sentinel data, checkout settings and Alertmanager secret volume names preserved |
| Outside-root invocation | `Push-Location ..`; invoke wrapper by absolute path; assert parsed app and Loki build contexts | Passed; correct repository-relative paths |
| Failure propagation | `.\scripts\compose.ps1 config --sentinel-invalid-checkpoint-flag` | Expected failure, exit 1; correctly propagated |
| No-argument invocation | Capture `.\scripts\compose.ps1` output without truncating its pipe; assert Usage and exit 0 | Passed; help only |
| Whitespace | `git diff --check -- README.md scripts/compose.ps1` | Passed; existing line-ending warning only |

Two initial verification methods failed: a nested `powershell.exe -File` process was rejected by Windows AuthorizationManager, and piping help to `Select-Object -First 4` terminated the output early and produced a nonzero status. No execution policy was changed. Verification was completed in the existing PowerShell session; capturing complete help output returned 0. The supported documented command is direct PowerShell invocation, not nested `powershell.exe -File`.

**Closure:** All checkpoint 1.1 acceptance checks pass for the documented invocation. No builds, starts, fault injections or external writes occurred. This is Compose-entry-point verification, not application E2E verification. Next checkpoint **1.2** must make the container environment explicitly local-safe before starting incident writers.

## Checkpoint 1.2 — Explicit local-safe configuration closed

Date: **18 October 2026**, Asia/Calcutta.

**Change:** Added `extras/docker-compose.local.yml` and applied it last through `scripts/compose.ps1`. Explicit environment overrides select SQLite, disable ServiceNow, blank its instance/user/password, disable automatic PR creation and blank the GitHub repair token for both `sentinel` and `sentinel-webhook`. The host `.env` is unchanged. The README explains the local behavior and remaining startup gates.

**Compatibility:** Read-only GitHub deployment evidence uses `SENTINEL_GITHUB_DEPLOYMENTS_TOKEN`, which is retained. Confluence, Prometheus, Loki and LLM configuration are retained. LLM calls can still incur charges. Live-mode source repair reads through the disabled GitHub repair client and is therefore unavailable in this local profile. Demo-mode fixture source reading remains available. Restoring external repair actions requires a future explicitly authorized configuration; this checkpoint does not provide a bypass switch.

### Executed verification

| Command / check | Actual result |
|---|---|
| `.\scripts\compose.ps1 config --quiet` | Passed, exit 0 |
| Parse wrapper `config --format json` in memory; compare with the original four-file result plus exact expected overrides | Passed. Original settings explicitly enabled ServiceNow; both merged services disable it. Only seven environment overrides and the YAML extension are different; all other runtime configuration preserved. No resolved secrets printed. |
| Invoke canonical wrapper from parent directory and inspect selected nonsecret merged settings | Passed; safe overrides apply outside repository root |
| `python -m pytest tests/test_local_safety.py tests/test_github_pr_client.py -q --basetemp=.pytest-tmp/checkpoint-1-2-run` using repo-local TEMP/TMP | **4 passed in 0.10s** |
| `python -m pytest tests/test_local_safety.py tests/test_github_pr_client.py tests/test_tools.py -k 'local_compose or repair_pr or linked_issue or servicenow' -q --basetemp=.pytest-tmp/checkpoint-1-2-connectors` using repo-local TEMP/TMP | **6 passed, 24 deselected in 0.25s**; intentional targeted connector selection, not a full suite result |
| `git diff --check -- README.md scripts/compose.ps1 extras/docker-compose.local.yml tests/test_local_safety.py` | Passed; existing README line-ending warning only |

The new parametrized regression applies the local environment to deliberately enabled fixture credentials for each service, confirms both adapters become unavailable, and asserts any network attempt fails the test. Existing mocked GitHub and ServiceNow lifecycle/fallback tests still pass.

An initial configuration-equivalence assertion failed because Compose includes the new `x-local-environment` YAML extension in its JSON output. The diagnostic identified that extension as the only additional difference. The final check asserts the extension equals the exact expected overrides and verifies that the remaining runtime configuration matches the intended baseline.

**Closure:** Configuration merge and adapter guard behavior are verified. No containers were built, started or changed; no external API calls or writes were made. Runtime verification is still pending. Next checkpoint: **1.3 — required configuration and mounted webhook secret validation**, then external demo-volume prerequisites before startup.

## Checkpoint 1.3 — Required configuration and mounted secret validation closed

Date: **18 October 2026**, Asia/Calcutta.

**Reproduced configuration weakness:** The resolved webhook service had an environment token but no secret-file setting or `/run/secrets` mount; Alertmanager used a file in the external secret volume. Equality of those separate tokens was not enforced. A current token mismatch was not demonstrated; the independent configuration paths were confirmed before changing them.

**Fix:** The local overlay clears the webhook environment token and mounts the same external secret volume as Alertmanager, read-only. `SENTINEL_WEBHOOK_TOKEN_FILE` now points to the same `/run/secrets/sentinel_webhook_token`. Existing token contents were not changed. UI remote webhook health checks do not need this mount.

**Reusable command:** `.\scripts\compose.ps1 preflight` consumes resolved JSON in memory and checks required LLM key presence/non-placeholder value, known mode, local write guards, SQLite path, configured ports, external read-only shared-secret mounts and the checked-in Alertmanager credential path. It then probes the secret through the existing local image as UID 10001, with no network, a read-only filesystem/mount, no extra capabilities and no new privileges. Volume/image inspection precedes the mount; it does not create missing volumes or pull images. Output contains neither secret values nor hashes. Subprocesses have bounded timeouts.

### Commands and observed results

| Command / check | Observed result |
|---|---|
| Inspect selected resolved secret settings as booleans only | Before fix: webhook file setting false, env token true, secret mount false; Alertmanager mount true |
| `docker image inspect sentinel-agent:local --format '{{.Id}}'` and `docker volume inspect sentinel-agent_alertmanager-secrets --format '{{.Name}}'` | Existing local image and external secret volume available |
| `python -m pytest tests/test_preflight.py tests/test_local_safety.py -q --basetemp=.pytest-tmp/checkpoint-1-3-tests` with repo-local TEMP/TMP | **28 passed in 1.01s** |
| `.\scripts\compose.ps1 config --quiet` | Passed, exit 0 |
| `.\scripts\compose.ps1 preflight` with approved Docker access | Passed, exit 0: configuration checks and actual UID 10001 mounted secret readability/format probe |

Tests cover enabled-write settings, placeholder keys, invalid mode/backend/port, missing/writable/different mounts, non-external volume, secret absence/invalid size/whitespace/non-ASCII/placeholder content, missing image/volume, helper failure, timeout and a successful secret probe. Actual token-validation code is also executed against temporary fixture files and must produce no stdout/stderr.

**Limits:** Key presence does not establish provider authentication, credit balance, model availability or live connector readiness. The credential path check reads checked-in Alertmanager configuration; image rebuild remains necessary to establish final image contents. Existing shared token validity is checked, not rotated. The temporary helper container removed only itself; existing volumes and application containers were unchanged.

**Closure:** Required configuration and mounted secret checks pass with actionable failures tested. Next checkpoint: **1.4 — external demo/shared-volume initialization prerequisites**. Full application build, startup, post-fix suite and E2E remain pending.

## Checkpoint 1.4 — Checkout demo/shared-volume prerequisites closed

Date: **18 October 2026**, Asia/Calcutta. Details and future build command: `docs/CHECKOUT_DEMO_PREREQUISITES.md`.

**Inspected:** sibling checkout Dockerfile, Compose, application settings/pricing handlers, tests, current container mount and selected nonsecret metadata. The existing `checkout-demo-settings` volume is mounted at `/config`; Sentinel mounts the same volume at `/demo-config`. UID 10001 can read/write settings, read pricing and write the directory. Health returned 200; demo state reported `fault_injected=false`, `demo_faults_enabled=true`. Current configured error rate is 0.0.

**Root cause reproduced:** initialization copied only pricing, despite Compose directing settings reads to `/config/settings.json`. A temporary container using empty tmpfs reproduced missing settings and HTTP 500 from `read_settings`, while the liveness function still returned OK. No real volume was mounted for this reproduction.

**Focused fix in `../checkout-demo-app`:** added `app/init_config.py`, changed Dockerfile startup to invoke it, prepared the image directory for UID 10001, and added `tests/test_init_config.py`. Initialization writes only missing files through completed temporary files and atomic create-if-absent links. Existing content, metadata and corrupt/symlink destinations are preserved. No working file on the real shared volume was modified.

| Executed command / method | Observed result |
|---|---|
| `docker inspect checkout-demo-app-checkout-demo-1` selecting image/mounts; `docker exec ... python -c ...` for metadata/health | Existing image/mount/settings and health verified |
| `docker run --rm --pull never --network none --read-only --tmpfs /config:rw ...` executing original pricing-only initialization | Reproduced: pricing present, settings absent, settings read HTTP 500, liveness OK |
| Host pytest targeting new checkout tests | Blocked at collection: `ModuleNotFoundError: fastapi`; no tests passed through this attempt |
| `docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw --mount 'type=bind,src=C:\Users\kvjha\Downloads\checkout-demo-app,dst=/workspace,readonly' --workdir /workspace -e PYTHONDONTWRITEBYTECODE=1 --entrypoint python checkout-demo-app-checkout-demo -m pytest tests -q -p no:cacheprovider --basetemp=/tmp/checkpoint-1-4-tests` | **12 passed, 1 upstream AnyIO/Starlette deprecation warning in 0.38s**, using existing checkout Python 3.12 dependencies |
| Isolated new initializer against empty tmpfs owned by UID 10001, edited checkout source mounted read-only | Passed: both files seeded, safe settings read, US pricing, idempotence, UID 10001 settings write and atomic code replacement |
| `docker exec --user 10001:10001 ...` with `os.access` and read-only `/demo/state` request | Existing volume permissions passed; pricing hash unchanged from initial inspection |
| `git -C C:\Users\kvjha\Downloads\checkout-demo-app diff --check` | Passed |

**Limits:** The running demo and images were not rebuilt or restarted. The fixed source was tested inside the existing image using read-only bind mounts. Fresh-directory ownership was modeled with tmpfs; actual rebuilt image and real volume first-start behavior are reserved for the build/start checkpoint. No persistent test volumes were created/deleted. Shipped defaults disable fault injection; existing enabled demo settings were preserved. No live fault or external API write occurred.

**Closure:** Existing demo/shared storage prerequisites pass and missing-file initialization is covered by regression and isolated functional tests. Next checkpoint: **1.5 — image versions, ports, DNS/host gateway paths and health dependencies**. Phase 1 as a whole is still open.

## Checkpoint 1.5 — Versions, ports, networking and health dependencies closed

Date: **18 October 2026**, Asia/Calcutta.

### Version and topology evidence

| Image / process | Installed version observed | Boundary |
|---|---|---|
| `sentinel-prometheus:v3.5.0-local` | Prometheus 3.5.0 | Matches Dockerfile version tag |
| `sentinel-alertmanager:container` | Alertmanager 0.28.1 | Matches Dockerfile version tag |
| `sentinel-loki:3.7.7` | Loki 3.7.7 | Matches Dockerfile version tag |
| `sentinel-alloy:v1.20.1` | Alloy v1.20.1 | Matches Dockerfile version tag |
| Sentinel and checkout bases | `python:3.12-slim` in Dockerfiles | Minor-version tag, not immutable patch/digest pin; final rebuilt runtime checked later |

All configured host port publications resolve to loopback; webhook 8090 is private to Docker networks. Current listeners are checkout 8000, Loki 3100 and Alloy 12345. Sentinel, webhook, Prometheus and Alertmanager remain stopped. No evidence of a conflicting extra published container was found in `docker ps -a`; actual host-bind availability will be established by startup.

Temporary probes confirmed:

- From `sentinel-agent-runtime` with configured host-gateway alias: checkout `/health` HTTP 200 and host-published Loki `/ready` HTTP 200.
- From `sentinel-agent_default`: service DNS for `loki` and `alloy`, Loki `/ready` HTTP 200, Alloy `/-/ready` HTTP 200 and checkout `/metrics` HTTP 200 through `host.docker.internal`.
- A separate default-network probe without an added host alias also resolved `host.docker.internal` and fetched checkout metrics HTTP 200, matching Prometheus's current Docker Desktop configuration.

These host-gateway results establish the current Windows Docker Desktop setup, not portability to arbitrary Linux hosts. In-stack DNS for stopped application services cannot be proven until they start. Existing working Loki/checkout host routes were preserved.

### Focused change

Source/image inspection showed no Docker healthcheck on Prometheus or Alertmanager, and Prometheus depended only on Alertmanager start. Added bounded `/bin/wget` checks against their local `/-/ready` endpoints in `extras/docker-compose.alerting.yml`, and changed Prometheus's dependency to `service_healthy`.

The resulting graph is acyclic: **UI healthy → webhook healthy → Alertmanager healthy → Prometheus start**. UI remains able to start without monitoring, avoiding a dependency cycle. Loki and Alloy images lack the chosen wget utility; no broken health command or new runtime package was added to them. Their actual readiness endpoints were probed externally and will be checked again after startup.

### Executed commands and outcomes

| Command / method | Observed result |
|---|---|
| `docker image inspect` selecting User/Entrypoint/Healthcheck; `docker ps -a --format ...` | Metadata and current bindings captured without environment secrets |
| Temporary `docker run --rm --pull never --network none --read-only --entrypoint <binary> <image> --version` (Loki uses `-version`) | All four monitoring versions matched |
| Temporary Python health/metrics/DNS probes on existing networks | All paths listed above passed |
| Native `/bin/wget -q -O /dev/null -T 2` in Prometheus and Alertmanager images, against ready Loki and unreachable localhost port 1 | Both images returned 0 for ready and 1 for unavailable |
| Prometheus `/bin/promtool check config /etc/prometheus/prometheus.yml` | Exit 0 |
| Alertmanager `/bin/amtool check-config /etc/alertmanager/alertmanager.yml` with read-only existing secret mount | Exit 0 |
| Alloy `/bin/alloy validate /etc/alloy/config.alloy` | Exit 0 |
| Loki `/usr/bin/loki '-config.file=/etc/loki/local-config.yaml' '-verify-config=true'` | Exit 0, config valid |
| `python -m pytest tests/test_compose_contracts.py tests/test_preflight.py tests/test_local_safety.py -q --basetemp=.pytest-tmp/checkpoint-1-5-tests` with repo-local TEMP/TMP | **32 passed in 1.05s** |
| `.\scripts\compose.ps1 config --quiet` plus assertions on parsed merged dependencies/ports | Passed; acyclic healthy dependencies, loopback-only publications, no webhook publication |
| `.\scripts\compose.ps1 preflight` | Passed after the dependency changes |

The initial unquoted Loki dotted flag was parsed incorrectly by PowerShell and failed before validation (`flag provided but not defined: -config`). Rerunning with quoted dotted arguments passed. The log distinguishes the failed invocation from the successful validator result.

**Closure:** Static contracts, installed tool versions, current network paths and native configuration validation pass. New healthchecks are not yet installed in running services; their own endpoints and healthy transitions remain build/start checks. Temporary containers removed only themselves and no application services, data or persistent volumes changed. Next checkpoint: **1.6 — build-context exclusion verification**, then **1.7 — build/start and inspect artifacts**.

## Checkpoint 1.6 — Docker build-context exclusions closed

Date: **18 October 2026**, Asia/Calcutta.

**Root cause reproduced:** Docker's root-relative ignore patterns did not exclude equivalent nested paths. Actual BuildKit context inspection exposed nested Python caches; harmless generated canaries demonstrated nested `.env`, `.envrc`, secret directories, logs, key files and environment directories could also be included. Existing recursive DB and backup exclusions worked. Initial Sentinel audit: **98 findings across 218 files**, exit 1. Checkout's context also included **4 nested cache files across 22 files**, exit 1. No matched credential values were printed.

**Fix:** Added recursive exclusions in both repositories' `.dockerignore`, preserving `.env.example` and application files. Added `scripts/audit_build_context.py`, `extras/ops/context-audit.Dockerfile` and regression tests. The audit uses Docker's actual filtered context, not an approximation of Docker pattern matching.

| Executed command / method | Observed result |
|---|---|
| Network-disabled BuildKit `RUN --mount=type=bind,target=/context,readonly` with `--output type=cacheonly`, original exclusions | Reproduced Sentinel's 98 findings; no context files copied into an output layer |
| Same audit after exclusions changed, before canary cleanup; explicit retained-file assertions | Passed: **121 files**, forbidden canaries absent; application files and allowed control/example canaries retained |
| `python -m pytest tests/test_build_context.py -q --basetemp=.pytest-tmp/checkpoint-1-6-tests` | **25 passed in 0.08s** |
| `docker build --file extras/ops/context-audit.Dockerfile --network none --no-cache --progress plain --output type=cacheonly .` after cleanup | Passed: **120 files**, no forbidden artifacts or recognized credential/private-key patterns |
| Equivalent BuildKit audit of checkout context, same scanner embedded in temporary build definition | Before fix: 4 findings / 22 files, exit 1; after fix: **18 files**, no findings, exit 0 |
| `python -m pytest tests/test_build_context.py -q --basetemp=.pytest-tmp/checkpoint-1-6-final` after final scanner change | **25 passed in 0.06s** |
| `git diff --check` targeting changed Sentinel build-context files | Passed |

File counts describe the inspected snapshots; later safe documentation changes alter counts. An initial checkout output filter also printed BuildKit's repeated scanner source; the stricter follow-up printed only actual result lines. That source contained pattern definitions, not secret values.

**Preservation:** Seventeen harmless canary files were created under a unique directory. Cleanup verified their paths and unchanged marker bytes, then removed only those generated files and empty directories. No user files, keys, databases, volumes or application containers were removed/modified. Inspection used read-only context mounts and cache-only export; no application image tag was replaced. BuildKit may cache the filtered context as part of normal local build operation.

**Limits:** Pattern-based text scanning does not establish that every secret format is absent. This check does not audit Git history, existing image layers or arbitrary binary formats. Application images still need rebuilding and inspection. No application services were started and no model/API calls or external writes occurred.

**Closure:** Actual filtered contexts, retained-file controls and regression checks pass. Next checkpoint: **1.7 — build intended images, inspect final artifacts and start the local-safe stack**.

## Checkpoint 1.7 — Rebuild, artifact inspection and local-safe startup closed

Date: **18 October 2026**, Asia/Calcutta.

### Reproduced issues and focused resolutions

1. **Oversized HTTP request reset on Windows.** First full host suite: **1 failed, 131 passed in 14.45s**. The oversized-body test lost its connection instead of receiving HTTP 413. The rejection closed the connection with unread upload data. Changed the handler to flush/send the response, half-close writes, and drain at most 65537 bytes with a one-second deadline. Added a headers-only oversize regression to ensure clients cannot hold rejection open waiting for a body. No assertion was weakened.
2. **Port 8501 already occupied.** Initial Docker startup failed binding 127.0.0.1:8501. `Get-NetTCPConnection` and selected `Win32_Process` metadata identified PID 37564 as this repository's native `apps/sentinel/web.py` process. Confirmed `/api/state` reported no active investigation, revalidated process identity and repository path, then stopped only that duplicate and retried startup. Host data files were preserved.
3. **Stale Alloy image.** After initial successful startup, actual container/image identity comparison found only Alloy still using its old image. Targeted `--force-recreate --no-deps alloy` preserved its positions volume and brought it onto the rebuilt image. Final identity checks passed for all seven containers.
4. **Loki startup warmup.** First immediate readiness pass saw Loki HTTP 503 after other endpoints passed. Bounded retry after warmup returned 200; final checks require actual endpoint success rather than Compose's running-state display.

### Data protection and build evidence

- Preflight passed before build/start. Existing Docker SQLite contained **29 tickets**.
- Read-only SQLite backup attempts failed opening the WAL-mode file without sidecars, including as root. Both writers were stopped and only `sentinel.db` was present, so copied the quiescent database into `.secrets/checkpoint-1-7-backup/sentinel-<timestamp>.db`, verified source bytes unchanged and backup `PRAGMA integrity_check=ok`. The backup is Git/build-ignored; existing volumes/databases were not replaced or deleted.
- Recorded demo settings/pricing hashes before recreation and compared them afterward: both files unchanged.
- Built monitoring and Sentinel through `.\scripts\compose.ps1 build`; built checkout through its own Compose file. Refreshed the shared Sentinel image with `build sentinel` after the HTTP fix; dependency layer reused.
- Python container runtime observed: **3.12.15**. Rebuilt image identities at final checks: Sentinel `c816136f23bfc9a1`, checkout `64e04831e9c58333`, Prometheus `56580160f460485c`, Alertmanager `be5ad40dd5645a4c`, Loki `dc6d9f1d20c23967`, Alloy `cde9a5191be0e741` (short SHA-256 IDs).
- Rebuilt filesystem audit: **120 Sentinel application files**, **7 checkout application files**, no forbidden local artifacts or recognized credential patterns. Inspected image configuration without printing environment values: provider credential variables absent; Sentinel configured as UID 10001.

### Commands and actual results

| Executed command / check | Observed result |
|---|---|
| `.\scripts\compose.ps1 preflight` | Passed before and after startup |
| `.\scripts\compose.ps1 build` | Completed; declared dependency installation took about eight minutes, then image export |
| `docker compose -f ..\checkout-demo-app\docker-compose.yml build checkout-demo` | Passed |
| New checkout image initialization in isolated writable container layer, no real shared volume | Both defaults seeded, repeated initialization preserved files, UID 10001 ownership correct |
| Rebuilt checkout tests, actual image application with tests mounted read-only, `/tmp` tmpfs, network none | **12 passed, 1 upstream deprecation warning in 0.22s** |
| Host `pytest tests/test_stabilization.py -k 'body_size or oversized'` after fix | **2 passed, 15 deselected in 4.11s** |
| Host `pytest tests -q --basetemp=.pytest-tmp/checkpoint-1-7-host-final` | **133 passed in 12.66s** |
| `.\scripts\compose.ps1 build sentinel` followed by container `python -m pip check` | Build passed; **No broken requirements found** |
| `docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw,uid=10001,gid=10001 --entrypoint python sentinel-agent:local -m pytest tests -q -p no:cacheprovider --basetemp=/tmp/checkpoint-1-7-suite` | **133 passed in 8.59s** |
| `docker compose -f ..\checkout-demo-app\docker-compose.yml up -d --no-build --wait --wait-timeout 60 checkout-demo` | Healthy; existing externally shared volume produced an ownership-label warning and was preserved |
| `.\scripts\compose.ps1 up -d --no-build --wait --wait-timeout 120` | Initial port conflict, then passed after verified idle native process stopped |
| `.\scripts\compose.ps1 up -d --no-build --force-recreate --no-deps --wait --wait-timeout 45 alloy` | Passed; actual running image now matches rebuilt tag |
| Final image IDs, running/restarting/health inspection for seven containers | All match intended rebuilt images; all running, none restarting; configured healthchecks healthy |
| Checkout health, Sentinel healthz, Prometheus/Alertmanager ready, Loki ready, Alloy ready | **All HTTP 200** after warmup |
| Served root HTML, JS and CSS byte comparison with workspace | Passed |
| `/api/state` flags and internal webhook service DNS health | Passed: ServiceNow/GitHub writes disabled; webhook listening |
| Live SQLite integrity and ticket count | Passed: integrity OK, **29 tickets preserved** |
| Private webhook missing-auth request and authenticated malformed JSON | Expected **401** and **400**; no valid alert or investigation queued |

An intermediate image-inspection action was not executed because automatic approval review hit an account usage limit. Following the user's request to continue, the normal approved retry succeeded; no approval mechanism was bypassed. One first webhook probe had shell-quoting syntax failure before making requests; the corrected subprocess argument-list probe passed.

### Current state and limits

Open **http://localhost:8501/**. All seven containers run their rebuilt images. Loki and Alloy have no Docker healthcheck; their HTTP readiness was verified directly. Native and Docker Sentinel servers must not compete for port 8501. The old native database remains at its existing path; no host/container state migration was performed.

No controlled fault, valid incident webhook, LLM inference, remote ticket/PR write or browser interaction test was executed in this checkpoint. Package registry downloads and Docker pulls were part of the authorized builds. Pattern scanning is not a complete security audit; floating dependency ranges remain a reproducibility risk for later CI work. The existing checkout volume ownership-label warning is nonfatal and does not justify deleting/recreating that shared volume.

**Closure:** Builds, image inspection, full host/container suites, startup, endpoint contracts, write guards and preservation checks pass. Next checkpoint: **1.8 — fresh metrics and Alloy/Loki ingestion**; then **1.9 — restart/persistence** before closing Phase 1.

## Checkpoint 1.8 — Fresh metrics and Alloy/Loki ingestion closed

Date: **18 October 2026**, Asia/Calcutta.

**Baseline:** Prometheus listed checkout and itself as up, with no last-scrape errors. Checkout `/demo/state` reported no injected fault. A two-minute Loki query found a current checkout stream before the test. Existing metric/log transport appeared available; no production component was changed.

**Verification added:** `scripts/check_telemetry.py` is a standalone local integration check. It sends six successful US orders using a unique URI query marker and requires all of the following within a bounded wait:

- Accepted HTTP responses with the expected US total.
- Successful-checkout counter increases by at least six.
- Prometheus's stored metric timestamp and target scrape time are newer than the test start, with target health up and no scrape error.
- At least six corresponding current HTTP-200 log lines in Loki, with service `checkout-service`, environment `demo` and job `checkout-demo`.
- No raw log/credential content is printed. Absent or failed queries are not reported as zero-valued successful evidence.

### Executed checks and observed results

| Command / method | Result |
|---|---|
| Read Prometheus `/api/v1/targets`, checkout `/demo/state`, bounded recent Loki query | Healthy baseline established |
| `.\.venv\Scripts\python.exe scripts/check_telemetry.py` | **Passed**, six accepted orders, counter **0 → 6**, six matching current Loki lines in one stream |
| Prometheus metric sample timestamp | `2026-10-18T10:15:28.348000+00:00` |
| Target last-scrape timestamp | `2026-10-18T10:15:28.348871744Z` |
| Unique marker | `sentinel-checkpoint-1-8-0339b0de1f584265a949340ccb3a62e7` |
| `docker exec ...` invoking actual `PrometheusClient.query_range` and `LokiClient.query_range` inside Sentinel, using configured container endpoints | Both passed; successful samples and all six real probe lines readable |
| Count Alloy diagnostics since the current collector container's `StartedAt` | **0 stale-log rejection lines; 0 warning/error lines** |
| Read checkout Prometheus alert states and demo state after the probe | No pending/firing checkout alert; no injected fault |
| Read live SQLite ticket count | Still **29**; no incident created |
| `git diff --check -- scripts/check_telemetry.py` | Passed |

**Stale-log distinction:** Historical collector rejection messages from older runs do not indicate a current ingestion failure. This run proved delivery of uniquely identified new log events and found no current stale-sample rejections. Retention, old-sample limits and stored collector positions were not changed; no data was deleted to achieve a pass.

**Scope:** This was a healthy telemetry integration test, not an error incident. Error-only agent evidence selectors were not changed to treat successful traffic as error evidence. No valid incident webhook, fault, LLM call, ServiceNow write or GitHub write occurred. No application code/config change or image rebuild was necessary. The new host verification script is not yet included in the image built at checkpoint 1.7, and does not need to be for this host-side command. Existing host/Docker suites remain the last observed 133-pass results from 1.7; they were not rerun for this verification-only addition.

**Closure:** Current target scrapes, fresh metric samples, current Docker→Alloy→Loki delivery and Sentinel client connectivity all pass. Next checkpoint: **1.9 — restart/persistence and duplicate-investigation checks**, then close Phase 1.

## Checkpoint 1.9 — Restart, persistence and duplicate prevention closed

Date: **18 October 2026**, Asia/Calcutta.

### Controlled live restart

Before restart, the UI reported idle, no running conversations were persisted, all four alert jobs were completed and the demo fault was inactive. A consistent read transaction captured logical hashes/counts of all six SQLite tables; nonsecret snapshots were stored under ignored `.secrets/checkpoint-1-9-snapshot.json`. Also captured container/volume identities, a stored Prometheus sample and six uniquely marked Loki events from checkpoint 1.8. No rows or test incidents were inserted into the real ledger.

Executed:

```powershell
docker compose -f ..\checkout-demo-app\docker-compose.yml restart --timeout 30 checkout-demo
.\scripts\compose.ps1 restart --timeout 45
.\scripts\compose.ps1 up -d --no-build --wait --wait-timeout 120
```

All seven original containers had changed `StartedAt` values but unchanged container IDs and volume mounts after this restart. SQLite integrity passed; every row/hash in the following tables was identical:

| Table | Preserved rows |
|---|---:|
| schema_migrations | 2 |
| tickets | 29 |
| incident_history | 32 |
| audit_events | 308 |
| conversations | 2 |
| alert_jobs | 4 |

The exact pre-restart historical Prometheus value/timestamp and all six old Loki events were preserved. A new healthy telemetry probe passed after restart: counter 0 → 6, six fresh log events, marker `sentinel-checkpoint-1-8-616266bbd5d94c6d8fbc3c89947d63ac`, scrape `2026-10-18T10:20:28.371980136Z`. Application counters reset with checkout process restart as expected; stored Prometheus history did not disappear.

### Failures reproduced and fixes

**Resolved ticket swallowed a new episode.** New fresh-process SQLite regression initially produced **1 failed / 2 passed in 11.52s**. Additional append-level regressions reproduced the defect in both backends (**2 failed, 3 deselected in 0.42s**). `find_by_fingerprint` excluded resolved tickets, but `append`'s transactional race guard did not. Updated the JSON candidate filter and SQLite query to exclude `Resolved` consistently. Active-episode duplicates remain suppressed; resolved episodes can create a new ticket. No schema migration or existing-data rewrite was needed.

**Transient Windows atomic-replace access error.** After the ticket fix, a full host run produced **1 failed / 137 passed in 23.93s**, failing the existing atomic pricing-file replacement with WinError 5. The test target was not read-only; a focused recheck passed (**1 passed in 0.07s**). The specific locking process was not identified. Added a bounded five-attempt retry for Windows rename/access/share errors only (0.3 seconds total sleep), unique temporary files, flush/fsync, preservation of existing mode bits, and cleanup. Permanent/non-Windows errors still raise and preserve original contents; no in-place replacement fallback or assertion weakening was used. Existing cleanup assertions remain present.

### Executed regression and release checks

| Check / command | Actual result |
|---|---|
| `pytest tests/test_restart_persistence.py -q --basetemp=.pytest-tmp/checkpoint-1-9-fixed` | **5 passed in 10.06s** after ticket fix |
| `pytest tests/test_code_fix.py tests/test_restart_persistence.py -q --basetemp=.pytest-tmp/checkpoint-1-9-affected` | **21 passed in 10.60s** before final mode-preservation case |
| Intermediate full host suite after atomic retry fix | **141 passed in 27.39s** |
| Original atomic cleanup regression rechecked | **1 passed in 0.12s** |
| Final `pytest tests -q --basetemp=.pytest-tmp/checkpoint-1-9-release-host` | **142 passed in 22.19s** |
| `.\scripts\compose.ps1 build sentinel` | Passed; existing dependency layer reused |
| Rebuilt image full suite: `docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw,uid=10001,gid=10001 --entrypoint python sentinel-agent:local -m pytest tests -q -p no:cacheprovider --basetemp=/tmp/checkpoint-1-9-release` | **142 passed in 15.11s** |
| Rebuilt image `scripts/audit_build_context.py /app` | **122 files**, no forbidden artifacts/recognized credential patterns |
| Idle-state guard then `.\scripts\compose.ps1 up -d --no-build --wait --wait-timeout 120` | Passed; tested image deployed to UI and webhook, existing volumes preserved |
| Repeat six-table logical comparison after completed deployment | All original rows/hashes unchanged; integrity OK |
| Second healthy telemetry probe during UI/webhook redeploy | Passed: counter 6 → 12, six fresh lines, marker `sentinel-checkpoint-1-8-7a973cd7fc504bc88f04691934e2fc36`, scrape `2026-10-18T10:25:58.353141569Z` |
| Preflight and final readiness | Preflight passed; three consecutive rounds of HTTP 200 from all six public readiness endpoints, five seconds apart |
| Final state checks | Idle UI, private webhook listening, ServiceNow/GitHub writes disabled, no fault, no pending/firing checkout alert |
| `git diff --check` on code/tests | Passed; line-ending warnings only |

The new process tests create isolated SQLite fixtures in child process A, then use a genuinely fresh process B to restore conversations/jobs and suppress repeated fingerprints. Worker/model execution is forbidden on the duplicate path. The interrupted-conversation case adds one honest interruption note, not a fabricated result; resolved-fingerprint recurrence creates a new local ticket. Other new cases assert transient retry success, permanent failure, non-Windows immediate failure, preserved originals, cleanup and file mode preservation. No valid incident was sent to the live webhook.

### Verification limitations and operational notes

- One post-deploy status check ran before container recreation completed and failed its running-state assertion. After the `up --wait` process completed, all volume/image/runtime comparisons passed.
- A later readiness sweep encountered one HTTP 503 without identifying which endpoint. Immediate named diagnostics showed all 200; three subsequent consecutive full sweeps passed. The transient's exact source was not established; a long-term availability claim is not made.
- Production records remained unchanged throughout; synthetic fixture tickets exist only in disposable test databases. No real ticket, PR, fault, LLM call or external message was created.
- This verifies controlled idle restarts and fixture-based fresh-process behavior. Durable replay of arbitrary interrupted alert jobs, multi-instance exactly-once delivery and external write reconciliation remain Phase 4 work. The local thread queue is not a distributed durable task queue.

**Closure:** Checkpoint 1.9 and **Phase 1** are complete. Local build/start, image safety, telemetry, controlled restart/persistence, process-level duplicate checks and final regression suites pass. Next: **Phase 2 checkpoint 2.4 — remaining HTTP/API edge cases**; baseline 2.1–2.3 was already verified during Phase 1.

## Checkpoint 2.4 — HTTP boundary validation and recovery (18 October 2026)

**Status: CLOSED.** Phase 2 remains in progress. Next: 2.5, trusted incident/session binding across concurrent investigations.

### Reproduced causes and focused fixes

- The webhook used the first Content-Length without rejecting duplicate lengths or simultaneous Transfer-Encoding. Otherwise valid test JSON was accepted with HTTP 202 in both cases. It also accepted valid JSON from an incomplete declared upload. These are now HTTP 400 before queueing.
- Missing/negative webhook lengths previously returned size error 413; malformed framing now returns 400. Actual zero/oversized body limits retain 413.
- The webhook socket had no read timeout: a client withholding the rest of the body could occupy a request thread indefinitely. Added a 10-second socket inactivity timeout and explicit HTTP 408 handling. This is an inactivity bound, not an absolute wall-clock upload deadline or a global request/thread capacity limit.
- The UI already had a 10-second read timeout. Extracted its unchanged value to REQUEST_TIMEOUT_SECONDS so both handlers can exercise timeout behavior quickly in isolated tests. The initial UI timeout test failed because its newly introduced override did not yet exist; that result did not establish a pre-existing missing UI timeout.
- Tests cover malformed UTF-8/JSON, non-object JSON, framing, truncated valid JSON, stalled bodies and subsequent health, absent credentials/header, unknown routes, unsupported PUT (existing 501), successful mocked dispatch, and concurrent invalid requests/health reads. Mocked dispatch never invokes a model, opens a ticket, or writes externally. This does not yet verify simultaneous investigation ownership (2.5).

### Actual commands and results

| Command/check | Observed result |
| --- | --- |
| Initial `pytest tests/test_http_edges.py -q --basetemp=.pytest-tmp/http-edges-red` | 4 failed, 22 passed; exposed status inconsistency and timeout test failures |
| Strengthened valid-payload reproduction `pytest tests/test_http_edges.py -q -k 'framing or truncated' --basetemp=.pytest-tmp/http-framing-red` | 5 failed, 7 passed, 14 deselected; proved ambiguous framing and truncation accepted |
| `pytest tests/test_http_edges.py tests/test_stabilization.py -q --basetemp=.pytest-tmp/http-edges-green` | 43 passed in 24.66s, before adding two successful-dispatch cases |
| `.\.venv\Scripts\python.exe -m pytest tests -q --basetemp=.pytest-tmp/checkpoint-2-4-host` | **170 passed in 38.88s**, workspace TEMP/TMP isolated |
| `.\scripts\compose.ps1 build sentinel` | Passed; dependency layer reused |
| `docker run --rm --pull never --network none --read-only --tmpfs /tmp:rw,uid=10001,gid=10001 --entrypoint python sentinel-agent:local -m pytest tests -q -p no:cacheprovider --basetemp=/tmp/checkpoint-2-4` | **170 passed in 30.10s** |
| Idle check, fingerprint six existing SQLite tables, then `.\scripts\compose.ps1 up -d --no-build --wait --wait-timeout 120` | Passed after container recreation; existing volumes retained |
| Post-deploy logical table fingerprint comparison | All six unchanged: migrations, tickets, incident_history, audit_events, conversations, alert_jobs |
| Six named readiness endpoints | All HTTP 200: checkout, UI, Prometheus, Alertmanager, Loki, Alloy |
| `.\scripts\compose.ps1 preflight` | Passed local write guards/configuration and readable shared webhook secret; no credential value printed |
| Live authenticated incomplete-body request inside private webhook container | HTTP **408 after 10.01s**; subsequent health **200**, no queued incident |
| Running UI/webhook image comparison | Both healthy on tested image `sha256:1225646aa895ec664f6a2973fb057865170b8d8bfe931c7bf6d110c5d79b2895` |
| `git diff --check` | Passed; existing CRLF conversion warnings only |

The live timeout probe loaded the mounted credential only inside the container, withheld a body byte stream, and printed status/timing only. The private before-deploy table fingerprint file is ignored at `.secrets/checkpoint-2-4-before.json`. No credentials, records, volumes or source databases were removed. No real valid incident, model call, remote ticket, PR or Confluence update was created. Browser responsiveness and full incident flow remain pending later checkpoints.
