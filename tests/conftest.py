"""Makes the repo root and apps/sentinel importable so tests can use `coded_tools.sentinel.*` and `alerts`."""

import sys
import os
from pathlib import Path

# Keep the default unit suite offline even when a developer's .env contains provider credentials.
for _name in (
    "CONFLUENCE_BASE_URL", "CONFLUENCE_CLOUD_ID", "CONFLUENCE_EMAIL",
    "CONFLUENCE_API_TOKEN", "CONFLUENCE_SPACE_KEYS",
):
    os.environ[_name] = ""
os.environ["SENTINEL_OFFLINE_MODE"] = "true"
# Existing adapter tests exercise the live connectors with mocked HTTP; demo-mode tests opt back in explicitly.
os.environ["SENTINEL_MODE"] = "live"
os.environ["SENTINEL_STORAGE_BACKEND"] = "json"
os.environ["SERVICENOW_ENABLED"] = "false"
os.environ["SENTINEL_AUTO_PR"] = "false"
for _name in ("SERVICENOW_INSTANCE", "SERVICENOW_USER", "SERVICENOW_PASSWORD", "SENTINEL_GITHUB_TOKEN"):
    os.environ[_name] = ""

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps" / "sentinel"))
