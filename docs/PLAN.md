# Sentinel — Hackathon Completion Plan

Last reviewed: **18 October 2026**. This is the implementation and verification checklist for the current repository, using the supplied architecture blueprint as a target rather than evidence of completed work.

## 1. Goal and definition of success

Deliver a reliable, demonstrable **Agentic AIOps and Incident Response Platform powered by Neuro-SAN**:

1. A controlled fault in the checkout demo produces metrics and logs.
2. Prometheus fires an alert; Alertmanager delivers an authenticated webhook.
3. Sentinel persists one incident, gathers attributable evidence, and retrieves a relevant runbook.
4. Deterministic checks evaluate the deployment hypothesis; the dashboard distinguishes verified facts from model narrative.
5. A supported incident produces a bounded repair proposal and regression evidence.
6. A human reviews the GitHub issue, PR, diff and checks before approving a merge.
7. Sentinel verifies recovery, records the outcome locally, and synchronizes an explicitly enabled ServiceNow instance.
8. Sentinel prepares a runbook improvement for review; publishing it to Confluence requires explicit approval.

**A successful investigation can also end with insufficient evidence.** Missing deployment records or an unavailable connector must prevent confident causal claims and unsafe repairs. The supported and insufficient-evidence paths are both presentation scenarios.

### Scope boundaries

- Keep the current HOCON network, Python tools, SQLite and HTML frontend. Stabilize existing components before replacing technology.
- Limit repair execution to the dedicated checkout demo repository and allow-listed files. Do not claim general autonomous production repair.
- Treat Confluence content, logs and repository text as untrusted data, never as instructions that authorize tools or writes.
- External writes, merges and runbook publishing require explicit authorization for the relevant destination. Previous local-only preferences remain in force.
- Preserve databases, volumes, user changes and ticket history. No destructive reset is part of this plan.
- Fixture evidence is supported and clearly labeled. Fixture mode can still require a live LLM; it is not automatically fully offline.

## 2. Status legend and evidence rules

| Status | Meaning |
|---|---|
| **DONE — implemented** | The code/configuration exists and was inspected. This alone does not establish current runtime health. |
| **DONE — verified** | A named check was executed and its outcome observed; record the revision and environment before release. |
| **IN PROGRESS** | Partial implementation or applied changes awaiting verification. |
| **TO BE STARTED** | No complete implementation was established in this review. |
| **BLOCKED** | A named prerequisite or execution permission prevents verification. |
| **DEFERRED** | Deliberately outside the core hackathon completion gate. |

Only mark a whole phase done after its acceptance checks pass. A configured API key is not proof of authentication, and a health endpoint is not proof of complete data flow.

### Sequential checkpoint protocol

We will close **one checkpoint at a time**, following the phase/task order below. Do not start dependent work while the preceding checkpoint is incomplete.

1. State the active checkpoint, scope and acceptance evidence.
2. Inspect and reproduce before making focused changes.
3. Execute the applicable checks; record commands, observed results and limitations in `docs/CHECKPOINT_LOG.md`.
4. Mark the checkpoint **DONE** only when its acceptance conditions are met. An unresolved failure stays **IN PROGRESS**; an unavailable prerequisite stays **BLOCKED**.
5. Ask the user when a scope choice, unavailable information or new external-write authorization is necessary. Routine reversible implementation choices use the existing agreed scope.
6. Report closure and identify the next checkpoint. Closure does not require an additional approval unless that next action itself needs authorization.

Checkpoint IDs use the phase number and task order, e.g. **1.1** for the canonical Compose entry point, **1.2** for the local-safe configuration. A pre-existing checkbox marked implemented is not evidence that the phase's verification gate passed.

**Current position:** **Phase 2 checkpoint 2.4 is closed**: HTTP boundary regressions and deployed webhook timeout/recovery pass; 170 tests passed on each host/Docker runtime. Phase 1 remains closed. Next: **checkpoint 2.5 — trusted incident/session binding during concurrent investigations**. Incident/browser E2E is still pending.

### Verification baseline inherited from stabilization

| Check | Last observed result | Interpretation |
|---|---|---|
| Existing Python suite | **59 passed in 6.22 seconds**, before the latest stabilization fixes | Valid baseline, not a post-fix release result |
| Dependency consistency | `pip check` passed | Installed metadata was consistent; not a vulnerability audit |
| New stabilization regressions before fixes | **15 failed, 1 passed** | Reproduced broken paths |
| Post-fix complete suite | **142 passed on host; 142 passed in rebuilt Docker image** | Latest checkpoint 1.9 includes fresh-process lifecycle and atomic replacement regressions; previous 1.7 result was 133 on each runtime |
| Combined four-file Compose validation | Passed before latest edits | Configuration parsed; build/start still needs verification |
| Current full Docker/browser E2E | **Pending** | Rebuilt stack/readiness, fresh telemetry and controlled restarts verified in 1.7–1.9; incident lifecycle and browser interaction checks remain |

No additional tests were run while writing this plan.

## 3. Architecture to preserve

```text
Checkout demo :8000
  ├─ metrics ─> Prometheus :9090 ─> Alertmanager :9093
  │                                    │ authenticated webhook
  └─ Docker logs ─> Alloy :12345 ─> Loki :3100
                                       │
                             Sentinel webhook :8090 (internal)
                                       │
                              Neuro-SAN / HOCON
            Commander → Logs / Metrics / Deployments / Runbooks
                              → Scribe → FixAgent → Responder
                                       │
                       Deterministic Python coded tools
                          ├─ SQLite persistent ledger
                          ├─ GitHub evidence + reviewed repair
                          ├─ Confluence retrieval
                          └─ optional ServiceNow synchronization
                                       │
                          HTML dashboard / API :8501
                                       │ human-approved merge
                        Demo repair sync → recovery verification
```

The UI and webhook are separate processes/containers sharing SQLite. Evidence adapters are modules, not independently deployed microservices. The monitored checkout app is a separate project/prerequisite.

### Current inventory

| Capability | Main files | Status / remaining work |
|---|---|---|
| HOCON agent definitions, roles, tool routing, middleware | `registries/sentinel.hocon`, `manifest.hocon`, `llm_config.hocon` | **DONE — implemented**; runtime routing regression pending |
| Neuro-SAN bootstrap | `apps/sentinel/run.py` | **DONE — implemented** |
| Bearer-protected webhook, deduplication, worker capacity limits | `apps/sentinel/alertmanager_receiver.py` | **DONE — implemented**; concurrency/restart verification pending |
| HTML dashboard and backend | `apps/sentinel/web.py`, `apps/sentinel/web/` | **IN PROGRESS**; API fixes unverified and disconnect UX incomplete |
| Prometheus rules, Alertmanager routing, Alloy/Loki | `extras/docker-compose.*.yml`, `extras/ops/` | **DONE — implemented**; complete stack restart and fresh telemetry pending |
| Pydantic incident/evidence models | `coded_tools/sentinel/schemas.py` | **DONE — implemented**; not universal validation of every request |
| Live/fixture evidence adapters | `prometheus_metrics.py`, `loki_logs.py`, `github_deployments.py`, `runbook_search.py` | **DONE — implemented**; prove live credentials and evidence in final rehearsal |
| Confluence scoped read adapter | `confluence_runbooks.py` | **DONE — implemented**, read-only |
| Deterministic hypothesis evaluation | `correlation_check.py`, `verified_facts.py`, `incident_context.py` | **DONE — implemented**; temporal support must not be described as causal proof |
| SQLite tickets, history, jobs, conversations, audit | `sqlite_store.py`, `ticket_store.py`, `incident_memory_store.py`, `conversation_store.py` | **DONE — implemented**; interruption and seed fixes **IN PROGRESS** |
| Optional ServiceNow create/update/resolve | `servicenow_client.py`, ticket tools | **DONE — implemented**; live writes not part of local-only test scope |
| Config proposal and bounded source repair | `remediation_pipeline.py`, `code_fix.py` | **DONE — implemented**, demo scope |
| GitHub issues, PR checks, binding and merge validation | `github_pr_client.py`, web repair endpoints | **DONE — implemented**; authorized live lifecycle pending |
| Incident memory and authoritative tool report | `record_incident.py`, `recall_incidents.py`, `finalize_incident.py` | **DONE — implemented** |
| CLI, tool tests and model evaluation harness | `apps/sentinel/alerts.py`, `tests/`, `eval/run_eval.py` | **DONE — implemented**; evaluator isolation must accommodate SQLite |

## 4. Blueprint reconciliation: what must not be overstated

| Blueprint claim | Current reality | Decision |
|---|---|---|
| FastAPI ingestion gateway | Sentinel uses Python `ThreadingHTTPServer`; checkout uses its own application stack | Keep existing gateway for hackathon; migration **DEFERRED** |
| Every tool/API payload uses Pydantic | Incident/evidence models exist; coverage is incomplete | Complete boundary validation incrementally in Phase 2 |
| State machine and durable task execution | Persistent records and state changes exist; workers are process-local threads | Add explicit transitions/interruption handling; do not claim a durable queue |
| Hard hop budgets and loop detection | HOCON execution time budget exists; general application-enforced budgets not established | Add deterministic limits in Phase 4 |
| Circuit breakers and exponential backoff | Request timeouts exist; a general retry/circuit layer does not | **TO BE STARTED**, Phase 4 |
| Secure sandbox | Restricted AST plus a separate Python interpreter and timeout | Describe accurately; stronger OS isolation in Phase 5 |
| Agent writes a reproducing unit test | Existing runner checks fixed supported/failing market cases | Generated, reviewed regression test artifact **TO BE STARTED**, Phase 5 |
| Complete ANSI/secret/context preprocessing | Redaction and bounded evidence exist; full ingress coverage not proven | Audit and test missing coverage in Phase 2 |
| CodeRabbit/Copilot review summaries | No integration established by this repository review | Optional Phase 9; never required for the base demo |
| Full GitHub Actions security/deploy pipeline | No `.github` workflow directory found here; external demo repo workflows not verified | Inspect both repositories and implement missing checks in Phase 6 |
| Recovery proves metric stabilization | Current code triggers recovery and probes checkout | Add a sustained metrics/log observation gate in Phase 7 |
| Incident learning updates Confluence | Current Confluence adapter only reads pages | Add approval-based proposal/publish flow in Phase 8 |
| Dependency vulnerability audit | Current config preflight checks a bundled advisory list | Label as scoped advisory check; real project dependency scanning in Phase 6 |

## 5. Incremental delivery sequence

Priority: **P0 = reliable and safe live presentation**, **P1 = complete proposed lifecycle**, **P2 = optional extension**. Finish P0 before broadening integrations. Each phase should remain reviewable and independently usable.

### Phase 0 — Freeze evidence and establish a reproducible baseline [P0]

**Status: DONE — baseline captured.** Closure evidence is in `docs/CHECKPOINT_LOG.md`. This closes discovery, not application verification; startup and regressions remain in Phases 1–2.

- [x] Map existing modules, HOCON roles, Compose services and shared stores.
- [x] Identify differences between the blueprint and actual implementation.
- [x] Reproduce initial failures before fixing them; record the baseline and regression results above.
- [x] Capture current revision, dirty diff summary, dependency versions, Docker state, networks, published ports and sanitized log tails.
- [x] Identify configured SQLite paths for host and container execution; preserve the alternative DB without merging or deleting it.
- [x] Record checkout demo repository/branch, shared-volume prerequisite, fault enablement requirement and recovery method.
- [x] Create a verification log containing commands, results, dates and blockers without secrets.

**Exit:** Another developer can reproduce the baseline without modifying user data or creating external tickets.

### Phase 1 — Make Docker startup deterministic and local-safe [P0]

**Status: DONE — verified.** Rebuild/start, artifact checks, live metrics/logs and controlled restart/persistence gates passed in checkpoints 1.1–1.9. Scope is local single-host operation, not durable replay of arbitrary interrupted agent jobs.

- [x] **1.1:** Provide one canonical startup command or wrapper combining the intended Compose files; verified wrapper `scripts/compose.ps1`, with README instructions and evidence in the checkpoint log.
- [x] **1.2:** Add `extras/docker-compose.local.yml` as the wrapper's final override; verify disabled ServiceNow/GitHub repair adapters and automatic PR creation despite enabled host settings. Six targeted connector/safety tests passed; read-only evidence configuration preserved.
- [x] **1.3:** Add `scripts/compose.ps1 preflight` with redacted actionable errors; share Alertmanager's read-only token mount with the webhook. Real configuration/UID 10001 secret probe passed; 28 targeted tests passed. Live API authentication remains a later check.
- [x] **1.4:** Document external checkout/shared-volume prerequisites; reproduce missing settings on first start and fix missing-file-only initialization in the checkout repository. Existing volume/UID access verified; 12 checkout tests passed. Empty-tmpfs checks passed; rebuilt image and real volume startup remain later gates.
- [x] **1.5:** Verify installed monitoring versions, loopback/private ports, current Docker Desktop DNS/host gateway paths and the acyclic health chain. Add Prometheus/Alertmanager readiness healthchecks and gate Prometheus on healthy Alertmanager. 32 targeted tests and four native configuration validators passed; full runtime readiness remains after startup.
- [x] **1.6:** Reproduce nested build-context leaks and add recursive exclusions in both repositories. Real BuildKit context audits pass (Sentinel 120 files, checkout 18 files at final inspection); 25 audit tests passed. Temporary canaries excluded while application/example controls retained; cleanup complete. Older image layers remain outside this check.
- [x] **1.7:** Rebuild and inspect both application images; all seven containers run intended rebuilt images. Six public readiness endpoints and private webhook checks pass. Host/container full suites each 133 passed; rebuilt checkout 12 passed; SQLite backup/integrity and 29 tickets preserved. Idle native port conflict resolved; no real fault or valid alert sent.
- [x] **1.8:** Six successful uniquely marked checkout requests produced fresh Prometheus counter samples (0 → 6) and six matching current Loki lines with correct labels. Sentinel's own client adapters read both sources. Current Alloy stale-rejection and warning/error counts are zero; no fault/alert/ticket created. Reusable verification: `scripts/check_telemetry.py`.
- [x] **1.9:** Seven containers restarted with original volumes; all six SQLite tables, historical Prometheus sample and six historical Loki events unchanged. Fresh ingestion recovered. Fresh-process isolated tests prove duplicate suppression and interrupted conversation restore. Fixed resolved-ticket append deduplication and bounded Windows atomic-replace errors; deployed tested image. Final suites: 142 host / 142 Docker passed.

**Exit:** Full local stack starts predictably, all required readiness checks pass, and no unapproved external writes occur.

### Phase 2 — Stabilize API contracts, state and dashboard [P0]

**Status: IN PROGRESS.** Existing regression patches were verified during checkpoint 1.7; further API edge cases and dashboard disconnect checks remain. Phase 2 itself is not closed.

- [x] **2.1:** Add baseline regressions for non-object JSON, oversized input, invalid investigation fields, active reset, restart recovery, seed flag, disabled ServiceNow, CLI imports and ticket binding; already verified during Phase 1.
- [x] **2.2:** Apply focused fixes for those paths; baseline regressions passed during checkpoint 1.7.
- [x] **2.3:** Rerun affected and complete suites; latest result after 1.9 fixes is 142 passed on each host/Docker runtime. Rerun affected/full suites again when subsequent Phase 2 code changes require it.
- [x] **2.4:** Add remaining HTTP cases: malformed encoding, incomplete body, timeout, bad auth, unknown route, unsupported input and concurrent requests. Added 28 real HTTP tests; fixed webhook framing/truncation validation and missing read timeout. Complete host/Docker suites: 170 passed each; live webhook returned 408 in 10.01s then health 200. See checkpoint log for limits.
- [ ] **2.5:** Audit trusted incident/session binding across manual and webhook investigations; prevent wrong-ticket updates during concurrent work.
- [ ] Show connection loss after any successful poll; disable unavailable actions, add fetch timeout and preserve failed chat input.
- [ ] Verify mobile/tablet/desktop layout, keyboard operation, loading/empty/error states and evidence links.
- [ ] Test raw alert, pasted log, connector result, exception and persistence redaction; add ANSI removal and length caps where absent.
- [ ] Confirm untrusted runbook/log text cannot alter service identity, authorize a merge or inject tool control data.

**Exit:** Web contracts behave predictably, UI status stays truthful, and restart/reset cannot detach or misbind an active incident.

### Phase 3 — Prove the existing alert-to-investigation path [P0]

**Status: TO BE STARTED for a fresh complete verification run.** Individual implementations are present.

- [ ] Start healthy baseline and capture current metrics/logs.
- [ ] Enable one approved reversible demo fault, produce bounded traffic and observe the actual Prometheus rule firing.
- [ ] Observe Alertmanager webhook authentication, acceptance, SQLite job creation and exactly one matching local ticket for repeated deliveries.
- [ ] Verify all four specialist evidence sources, query windows, timestamps, service/environment mapping and provenance in the UI.
- [ ] Use a genuine successful deployment record for the supported path. If authorized preparation is needed, record the actual deployment rather than manufacturing incident evidence.
- [ ] Exercise missing deployment, missing runbook, unavailable Loki/Prometheus and invalid credentials; require explicit unavailable/insufficient evidence.
- [ ] Verify final report, work notes, incident memory and follow-up ticket reuse.
- [ ] Recover the controlled fault and confirm telemetry returns to baseline.

**Exit:** A real webhook-triggered investigation is visible end to end, and unavailable evidence blocks unjustified repair.

### Phase 4 — Strengthen the agent harness and connector resilience [P0 minimum; P1 advanced]

**Status: TO BE STARTED for additional mechanisms.** Existing schemas, timeouts, evidence gates and worker limits remain the foundation.

- [ ] Define allowed job transitions (`queued → investigating → completed/failed/interrupted`) and auditable terminal reasons.
- [ ] On restart, mark abandoned work interrupted; support explicit resume/retry with stable incident identity instead of silent re-execution.
- [ ] Enforce request/run budgets for elapsed time, tool calls and evidence size; expose exhaustion without a fabricated success.
- [ ] Detect repeated identical tool calls and stop bounded loops; confirm framework capabilities before duplicating them.
- [ ] Add bounded retries with jitter for transient read failures and `429`/selected `5xx`, honoring `Retry-After` within the total deadline.
- [ ] Never blindly retry creates/merges/publishes: reconcile ambiguous outcomes using stable identifiers before another write.
- [ ] Add per-connector circuit open/half-open/closed behavior and visible degraded status; exercise recovery.
- [ ] Document LLM provider timeout/rate-limit/auth failure behavior, model selection, request budget and secret-free tracing.
- [ ] Keep connector configuration status distinct from actual readiness.

**Exit:** Outages and budget exhaustion produce bounded, auditable failure; recovery does not multiply tickets or writes.

### Phase 5 — Complete repair verification and two-layer testing [P0 existing checks; P1 stronger isolation/TDD]

**Status: IN PROGRESS.** Fixed local repair checks exist; generated regression artifacts and OS sandboxing are not done.

- [ ] Verify the existing syntax, allow-list, patch-size, signature, secret-pattern and functional checks against good and malicious/broken candidates.
- [ ] Add a regression artifact tied to the observed defect: run it against original and candidate; require original failure and candidate success plus preservation tests.
- [ ] Initially support a deterministic test template for the checkout defect; expose broader agent-written tests only after validating their contents and assertions.
- [ ] Prevent a generated test from being the sole repair oracle; retain trusted independently defined checks.
- [ ] Add stronger execution isolation where available: non-root worker, read-only root, temporary writable workspace, no network, CPU/memory/PID limits and enforced deadline.
- [ ] Never mount the Docker socket or application secrets into the repair runner. If secure isolation is unavailable, keep untrusted patch execution disabled or explicitly restricted to the trusted demo scenario.
- [ ] Persist original/candidate/test hashes, checks, duration and outputs with redaction.
- [ ] Ensure repeated generation is bounded and no failed candidate becomes PR-eligible.

**Exit:** The proposed fix demonstrably addresses the reproduced defect, preserves working cases and has a clearly stated isolation boundary.

### Phase 6 — Add enforceable repository CI and security checks [P0]

**Status: TO BE STARTED in this repository.** Existing GitHub client checks are implemented; external demo CI needs inspection.

- [ ] Inspect existing checkout demo Actions before adding duplicate workflows.
- [ ] Add Sentinel CI for the supported Python version, unit/integration regressions and Compose validation with no live write credentials.
- [ ] Add meaningful static checks, secret scanning and dependency vulnerability scanning; identify each tool and its real coverage.
- [ ] Extend secret scanning beyond staged files for the final working-tree review; never print matched credential values.
- [ ] Test both local Python and Docker Python versions or standardize the development runtime.
- [ ] Keep dependency changes minimal; create a reproducible dependency installation strategy after compatibility verification.
- [ ] Make demo PR functional/security checks visible; verify branch/check names and failure/pending/absent behavior.
- [ ] Validate incident binding, expected base/head SHA, allowed changed paths and content hashes immediately before merge.
- [ ] Rotate previously exposed credentials and verify `.env`, traces, backups and DBs are excluded from Git/build artifacts.

**Exit:** A deliberately bad repair or leaked test secret fails the applicable check; no assertions are weakened to make CI green.

### Phase 7 — Verify approved repair, recovery and ticket reconciliation [P0]

**Status: IN PROGRESS.** PR/recovery code exists; complete authorized rehearsal and sustained telemetry gate pending.

- [ ] Use the dedicated demo repository and explicitly authorized write scope to create one incident-bound issue/PR.
- [ ] Demonstrate diff, evidence and all required CI checks before an explicit human approval.
- [ ] Revalidate the PR after approval; refuse changed heads, wrong service/session, failed/pending checks or out-of-scope files.
- [ ] Synchronize only the validated merged repair; audit the applied artifact and merge SHA.
- [ ] Verify checkout success across formerly failing and previously working inputs.
- [ ] Observe enough traffic and fresh Prometheus samples over a documented recovery window, checking error rate and relevant new logs; one successful probe is insufficient.
- [ ] Resolve only after recovery passes. Leave the incident open when evidence is missing or the service relapses.
- [ ] Persist separate local recovery and external synchronization status so ServiceNow outages can be reconciled without repeating deployment/merge.
- [ ] Test restart or timeout between merge, apply, recovery and remote update; show a safe retry/finalize operation.
- [ ] If the demo fails, use a documented human-approved revert/recovery procedure; never hide failed probes.

**Exit:** An approved fix produces measured recovery, traceable resolution and safe handling of partial external failure.

### Phase 8 — Close the knowledge loop with reviewed Confluence updates [P1]

**Status: TO BE STARTED.** Existing read-only runbook lookup is done and remains available.

- [ ] Build a draft incident learning note from verified facts: symptoms, queries, cause confidence, approved fix, checks, recovery and links.
- [ ] Preview it locally first and store proposal/review status in SQLite; do not require a Confluence write token for draft generation.
- [ ] Let a human select a permitted target page and review a proposed appended incident section or child-page draft.
- [ ] Add a narrowly scoped publish adapter behind explicit approval and configured write authorization.
- [ ] Use current Confluence page version checks to avoid overwriting concurrent edits; reconcile retries by incident marker.
- [ ] Audit approval, target, version and result; sanitize the content and preserve existing page material.
- [ ] Test `401/403`, stale version conflicts, timeouts, repeated publish and rollback/revert instructions.

**Exit:** A resolved incident yields a reviewable knowledge artifact; an authorized publish occurs once without damaging the existing runbook. If publishing cannot be completed, present the saved draft as pending approval.

### Phase 9 — Optional presentation enhancements [P2]

**Status: TO BE STARTED / DEFERRED until P0 passes.**

- [ ] Ingest CodeRabbit/Copilot review summaries only if available in the demo repository; show provenance and never replace deterministic checks with reviewer prose.
- [ ] Add Sentinel's own structured timing/health telemetry, with incident IDs and connector latency, without leaking prompts/secrets.
- [ ] Add authenticated multi-user access/RBAC before shared/public dashboard exposure; keep hackathon access local otherwise.
- [ ] Add Slack/Teams notification previews; actual messages require separate authorization.
- [ ] Defer Kubernetes, Datadog, vector databases, PostgreSQL and a distributed queue until a concrete requirement justifies them.
- [ ] Defer FastAPI migration unless current server limitations become an evidenced blocker.

## 6. Verification matrix

| Layer | Success case | Failure/recovery cases | Required evidence |
|---|---|---|---|
| Contracts/redaction | Valid incident and safe evidence | Bad types, empty fields, malformed timestamps, oversized/ANSI/secret-bearing logs | Executed unit regressions with sanitized artifacts |
| Ingestion | Valid bearer webhook accepted | Wrong token, bad JSON, duplicate, capacity, interrupted job | HTTP status/body, matching job/ticket IDs |
| Persistence | Ticket/session/audit survive restart | Concurrent updates, seed disabled, unavailable/locked store, alternate path | Isolated DB assertions; preserved real data |
| Connectors | Genuine bounded evidence | Auth failure, timeout, `429/5xx`, empty data, malformed payload, circuit recovery | Mocked transport tests plus separate authorized live probes |
| Agent network | Every specialist runs; tool verdict reported | Model/provider failure, repeat loop, unsupported/missing evidence | HOCON runtime trace and tool facts, not prose matching alone |
| Repairs | Reproduced bug fails before and passes after | Malicious import, infinite loop, broken good input, weak test, wrong file/hash | Check artifacts and bounded execution result |
| GitHub/recovery | Approved bound PR, checks, merge, stable recovery | Changed SHA, absent CI, rejected approval, failed apply, relapse, partial sync | Audit trail with live writes only when authorized |
| Browser | Real investigation and correct actions | Disconnect/reconnect, refresh, failure, keyboard/mobile, duplicate submit | Observed browser run and screenshots if useful |
| Knowledge | Sanitized reviewed draft | No credentials, publish conflict, repeated publish, remote failure | Local proposal plus authorized publish record |

Keep ordinary tests deterministic and separate from live model/API tests. Isolate evaluation state in a disposable SQLite DB and disable remote writes; the existing evaluator's JSON backup/restore description must not be assumed to protect SQLite state.

## 7. Command baseline and prerequisites

Run from the repository root in PowerShell. These are the current command forms; record actual outcomes when executed. They are not a claim that the current build passes.

```powershell
# Existing isolated test setup; new temporary test files only.
$taskTestTemp = Join-Path $PWD '.pytest-tmp\hackathon'
New-Item -ItemType Directory -Force $taskTestTemp | Out-Null
$env:TEMP = $taskTestTemp
$env:TMP = $taskTestTemp
$env:SERVICENOW_ENABLED = 'false'
$env:SENTINEL_AUTO_PR = 'false'
$env:SENTINEL_GITHUB_TOKEN = ''
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m pytest tests/test_stabilization.py -q --basetemp=.pytest-tmp/hackathon-regression
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp/hackathon-full

# Canonical local-safe Compose syntax validation without echoing environment secrets.
.\scripts\compose.ps1 config --quiet
```

**Docker launch gate:** The wrapper now applies the verified local-safe overlay, and `.\scripts\compose.ps1 preflight` checks required configuration and the mounted webhook secret. Demo-volume initialization, build and readiness checks remain pending in Phase 1 before starting incident writers. Shell variables alone do not reliably override values loaded into containers by `env_file`; use the canonical wrapper instead of the original four-file command.

After that gate, use the documented wrapper to build/start, inspect `compose ps`, and check UI `/healthz`, Prometheus `/-/ready` and `/targets`, Alertmanager `/-/ready`, Loki `/ready`, and webhook health internally. Open the UI at **http://localhost:8501/** rather than `file:///.../index.html`. Verify the checkout app at **http://localhost:8000/health**. Avoid unfiltered config/log output containing secrets.

## 8. Final hackathon release gate

- [ ] Full relevant test suite passes after the final code change; record passed/failed/blocked counts and exact commands.
- [ ] Clean Docker build and startup are reproduced with the canonical workflow.
- [ ] Real alert-to-ticket investigation and missing-evidence behavior are demonstrated.
- [ ] Human approval, repair checks and recovery are demonstrated within the authorized demo scope.
- [ ] Confluence retrieval works; update draft/publishing is honestly labeled according to Phase 8 status.
- [ ] UI remains truthful during dependency loss and reconnect.
- [ ] No unintended external writes, exposed credentials or deleted user data.
- [ ] README and architecture notes match the actual HTTP server, framework, live/fixture behavior, isolation limits and recovery mechanism.
- [ ] Prepare a short judge script: healthy baseline → fault → telemetry → alert → evidence → verdict → repair checks → human review → recovery → knowledge draft.
- [ ] Prepare a labeled fallback replay and saved sanitized evidence; never present a recording or fixture as a fresh live result.
- [ ] Preserve sanitized test/E2E artifacts and a concise list of remaining limitations.

## 9. Recommended next action

Phase 1 and Phase 2 checkpoint 2.4 are closed. Begin **checkpoint 2.5** for trusted incident/session binding across concurrent manual and webhook work. Then address dashboard disconnect/recovery and UI checks. Do not add optional integrations before the current live incident path is demonstrated. After P0 is verified, add the generated regression artifact and reviewed Confluence learning loop as the main P1 improvements.
