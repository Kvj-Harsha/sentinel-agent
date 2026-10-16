"""Makes backend/ importable so tests can use `coded_tools.sentinel.*` and `alerts` directly."""

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
os.environ["SENTINEL_STORAGE_BACKEND"] = "json"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
