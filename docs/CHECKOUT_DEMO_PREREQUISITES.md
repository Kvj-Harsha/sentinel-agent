# Checkout demo prerequisites

Checkpoint 1.4 verified on 18 October 2026. The checkout service is a separate repository at `../checkout-demo-app`, branch `main` (baseline HEAD `cd50b6e`). It is not built by Sentinel's Compose files.

## Shared files and permissions

| Consumer | Volume | Mount / path |
|---|---|---|
| Checkout | `checkout-demo-settings` | `/config/settings.json`, `/config/pricing.py` |
| Sentinel UI and webhook | Same external volume | `/demo-config/settings.json`, `/demo-config/pricing.py` |

Settings must be a valid JSON object containing the expected demo flags. Pricing must be readable Python source. Sentinel's UID 10001 needs write access to the settings file and volume directory for approved updates/atomic code replacement. Do not change ownership recursively or replace existing settings to make a check pass.

The existing volume passed these prerequisites: UID 10001 can read/write settings, read pricing and write the directory. Its observed settings had `demo_faults_enabled=true`, `checkout_error_rate=0.0`; `/demo/state` reported no active fault. These are observed values, not settings enforced by startup.

## First-start fix

The previous checkout Dockerfile seeded only pricing. On empty storage, `/health` still returned OK while settings access raised HTTP 500. The checkout repository now has:

- `app/init_config.py`: seed missing settings/pricing using a completed temporary file and atomic create-if-absent hard link; preserve every existing destination, including corrupt files and symlinks. Clean up only the initializer's own temporary file.
- Dockerfile: prepare the image's `/config` directory for UID 10001, run initialization before Uvicorn, then serve normally.
- `tests/test_init_config.py`: cover first start, idempotence, unchanged existing content/metadata, concurrent initialization and failed source reads.

New files created by root initialization belong to UID 10001. Existing volume ownership and existing repaired files are not modified. If existing storage has wrong permissions or corrupt content, diagnose it and agree on a repair instead of silently resetting it.

The shipped settings disable fault injection. Starting from an empty volume therefore produces a safe default, not an incident-ready demo. Enabling a fault and producing traffic is a later controlled E2E action.

## Commands and scope

Inspect without changing state:

```powershell
docker compose -f ..\checkout-demo-app\docker-compose.yml ps
curl.exe --fail http://localhost:8000/health
curl.exe --fail http://localhost:8000/demo/state
.\scripts\compose.ps1 preflight
```

The checkout image must be rebuilt later to include the fix. At the build/start checkpoint, the non-destructive command from Sentinel's root is:

```powershell
docker compose -f ..\checkout-demo-app\docker-compose.yml up -d --build checkout-demo
```

That command was **not executed in checkpoint 1.4**. Build/image contents and restart behavior remain later gates. The current running demo is unchanged.

`POST /demo/fault` requires `demo_faults_enabled=true` and starts bounded synthetic shopper traffic. `POST /demo/recover` clears the fault and stops that traffic. Neither endpoint was invoked in this checkpoint.

## Actual verification

- Original first-start logic reproduced in the existing image with an empty `/config` tmpfs: pricing present, settings absent, settings read HTTP 500, liveness OK.
- All checkout tests ran in its existing Python 3.12 image with the edited source mounted read-only and test data on tmpfs: **12 passed, 1 upstream deprecation warning in 0.38s**.
- New initializer executed against empty tmpfs: both defaults created; US pricing works; repeated initialization preserved bytes; UID 10001 settings write and atomic pricing replacement passed.
- The permission test models the fresh directory ownership prepared by the new Dockerfile. A rebuilt image's real volume initialization remains to be checked during build/start; no persistent test volume was created or deleted here.
- Host pytest attempt was blocked at collection because Sentinel's Python environment lacks FastAPI. No dependencies were installed; verification used checkout's own image.

No real settings, source in the shared volume, tickets or databases were changed, and no faults or external API writes were triggered.
