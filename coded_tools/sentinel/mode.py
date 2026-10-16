"""Single switch between the reproducible replay scenario and the optional live connectors."""

import os

MODE_ENV = "SENTINEL_MODE"


def demo_mode() -> bool:
    """
    True (the default) means evidence tools replay the bundled checkout-500 scenario from data/*.json and label it
    `fixture`. Set SENTINEL_MODE=live to query Prometheus, Loki, GitHub Deployments and Confluence instead.
    """
    return os.environ.get(MODE_ENV, "demo").strip().lower() != "live"
