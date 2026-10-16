# Local container operations

This profile packages Sentinel's Streamlit UI and Alertmanager receiver as separate containers. It is for a
single-machine demo. It does not provide production TLS termination, multi-host storage, or a managed secret store.

## Prerequisites and start

- Docker Desktop with Compose.
- A valid `.env` with the model provider settings needed by Sentinel.
- `SENTINEL_WEBHOOK_TOKEN` in `.env` and the matching token seeded into the existing
  `sentinel-agent_alertmanager-secrets` volume. Keep both copies out of Git, screenshots, and shell output. Compose
  passes the token to the local webhook through `.env`; Alertmanager reads its copy from the named volume.
- The external `sentinel-agent_alertmanager-secrets` volume seeded as documented in
  [the local alerting demo](LOCAL_ALERTING_DEMO.md).

From the repository root, start the app and monitoring services together:

```powershell
docker compose -f docker-compose.alerting.yml -f docker-compose.app.yml up -d --build
docker compose -f docker-compose.alerting.yml -f docker-compose.app.yml ps
```

The UI publishes only `127.0.0.1:8501`. The receiver has no host-published port; Alertmanager reaches it over the
private Compose network. Docker health checks verify the UI, writable SQLite initialization, and webhook health
endpoint. The receiver accepts at most four investigations concurrently; when full, it returns HTTP 503 with
`Retry-After` so Alertmanager can retry. Failed jobs remain visible in the UI for operator review.

## Stop and upgrade

Stop containers without deleting their data volumes:

```powershell
docker compose -f docker-compose.alerting.yml -f docker-compose.app.yml down
```

The named SQLite, Prometheus, Loki, Alloy, and Alertmanager secret volumes persist. Do not add `-v` unless you intend
to delete them. To upgrade, review the code/config change, rebuild, and recreate the stack with the start command.
Wait for `sentinel` and `sentinel-webhook` to report healthy before sending alerts.

Docker sends SIGTERM to the webhook process. It stops accepting requests and gives active investigations up to 30
seconds to finish within the 45-second container stop grace period. If the deadline expires, the receiver logs the
number of still-running jobs; retry or investigate those alerts after restart.

## Backup and recovery

Before resetting or removing volumes, stop all writers and export the persistent data volume to a protected host
directory. For example, set `$backupPath` to a private absolute directory, then use a short-lived helper container:

```powershell
docker compose -f docker-compose.alerting.yml -f docker-compose.app.yml stop sentinel sentinel-webhook alertmanager
$backupPath = (New-Item -ItemType Directory -Force .\backups).FullName
docker run --rm --mount type=volume,source=sentinel-agent_sentinel-data,target=/source,readonly --mount "type=bind,source=$backupPath,target=/backup" alpine:3.20 sh -c 'tar -czf /backup/sentinel-data.tgz -C /source .'
```

Restrict access to the archive: it contains tickets, incident history, and audit details. Back up monitoring volumes
separately if their data matters. Restore by stopping all Sentinel writers and extracting the archive into the named
volume before starting the stack again. Verify the SQLite file and keep the backup outside Docker volumes before
removing any volume.

For a logical local reset while retaining a pre-reset backup in the SQLite volume, run the one-off container while
the app and webhook are stopped:

```powershell
docker compose -f docker-compose.alerting.yml -f docker-compose.app.yml run --rm --no-deps --entrypoint python sentinel backend/coded_tools/sentinel/reset_local_state.py --yes --database-only
```

The container's database-only reset preserves the read-only legacy JSON fixtures. It makes no provider API calls.

## Disable write integrations quickly

To stop new investigations and writes, stop Alertmanager and both Sentinel processes:

```powershell
docker compose -f docker-compose.alerting.yml -f docker-compose.app.yml stop alertmanager sentinel-webhook sentinel
```

To leave the UI available while disabling provider writes, set `SERVICENOW_ENABLED=false`, remove
`SENTINEL_GITHUB_TOKEN`, `SENTINEL_DEMO_GITHUB_OWNER`, and `SENTINEL_DEMO_GITHUB_REPO` from the runtime environment,
then recreate the containers. GitHub deployment lookup uses its separate read-only token. Revoke compromised or
unneeded credentials at the provider; removing an environment variable does not revoke a credential.

## Exposure boundary

Keep the UI loopback-only and the receiver on the Compose network. If a remote operator needs access, put the UI behind
an authenticated TLS reverse proxy and use a private network. Do not publish the receiver to the public internet.
For multiple app workers, replace the local SQLite volume with a shared transactional database and a durable queue;
this demo's SQLite and in-memory deduplication are single-host only. Keep the Docker socket mounted only for the local
Alloy collector and remove that collector/socket mount from environments that do not need it.
