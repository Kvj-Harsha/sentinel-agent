# Security, credentials, and local data retention

## Data handling

- Evidence is normalized through `EvidenceRecord`. Its `safe_fields` accepts a field allow-list, nested strings are scrubbed, and published verified facts are scrubbed before they reach the UI or agent result.
- Ticket text and incident-memory records are scrubbed before they are written. Ticket notes are scrubbed before either local persistence or an optional ServiceNow write. Provider error bodies and authorization headers are not included in connector errors.
- The SQLite database contains tickets, incident-memory records, and compact audit events. It is local, ignored by Git, and has no automatic expiry. Keep the app's `.env` and database on a trusted machine.
- The legacy `tickets.json` and `incident_history.json` remain as migration inputs; they can also contain incident text. Imported SQLite rows and data read through the JSON stores are scrubbed, but the JSON files on disk are rewritten only by the explicit scrub or reset command.
- Prometheus and Loki retain local monitoring data for 7 days. Their named Docker volumes persist across container restarts. The reset script does not remove those volumes.
- GitHub Issues and PRs created by the repair workflow remain in the configured demo repository until deleted there. Confluence is searched read-only. Any provider-side retention is controlled by that provider and its account settings.

## Reset local incident data

Stop Sentinel first, then run this from the repository root:

```powershell
python coded_tools/sentinel/reset_local_state.py --yes
```

The script backs up the SQLite database and legacy ticket/history JSON under `coded_tools/sentinel/data/.reset-backups/`, clears local tickets, incident memory, audit events, and legacy JSON, and makes no external API calls. Backups contain the same sensitive incident data; delete the backup directory when it is no longer needed. Resetting monitoring volumes is separate and destructive; inspect the Docker volumes before removing them.

To preserve records while applying the expanded PII/credential scrub to legacy JSON, run `python coded_tools/sentinel/scrub_local_state.py --yes` instead. It takes a pre-scrub backup first. Stop Sentinel before either maintenance command.

## Credential boundaries

Keep each credential separate and scoped to the demo:

| Connector | Credential and scope |
| --- | --- |
| LLM provider | A dedicated project key in `.env`; do not reuse a personal account key. |
| Confluence | A separate Atlassian API token for an account with read access only to the `cts` space. |
| GitHub deployment lookup | Optional fine-grained token with Deployments: read only. |
| GitHub repair workflow | A different fine-grained token or GitHub App installation restricted to the demo repository, with only Contents, Issues, and Pull Requests write plus Checks read. |
| ServiceNow | Disabled by default. If enabled later, use a development instance and a dedicated integration account with only the incident operations required by the demo. |
| Prometheus/Loki | Prefer local loopback endpoints. If secured remote instances are used, configure read-only credentials separately for each service. |

Never put credentials in HOCON, fixtures, ticket descriptions, URLs, or screenshots. `.env` is ignored by Git; `.env.example` contains placeholders only. Rotate a credential at its issuing provider before removing any accidental copies from files or history. Any credential pasted into a chat, log, screenshot, or tracked file should be treated as exposed until revoked.

## Secret and PII checks

Enable the repository's staged-file secret scan once per clone:

```powershell
git config core.hooksPath .githooks
```

The pre-commit hook runs `python scripts/secret_scan.py`. It checks staged blobs for common high-confidence token and private-key patterns, suppresses matched values, and blocks a commit on a match. This is a guardrail, not a complete secret scanner; review history with a dedicated scanner configured to redact matches before sharing a branch. Rotate first; history cleanup does not make an exposed credential safe again.

The expanded redaction rules and pre-commit scan do not revoke credentials already shared outside the repository. Revoke and replace any such keys at their issuing providers, then update only the corresponding local `.env` values.
