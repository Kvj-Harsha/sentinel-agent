"""Verify fresh healthy checkout traffic -> Prometheus and Docker -> Alloy -> Loki.

Run from the host against the local Docker stack. Sends six successful US demo
orders with a unique query marker; no faults, valid alerts, model calls or remote
ticket/PR writes. Prints counts/timestamps, never raw logs or credentials.
"""

from datetime import datetime, timezone
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import uuid


APP = "http://127.0.0.1:8000"
PROM = "http://127.0.0.1:9090"
LOKI = "http://127.0.0.1:3100"
COUNT = 6
SERIES = 'checkout_requests_total{job="checkout-demo",service="checkout-service",environment="demo",status="ok"}'


def read(url, body=None):
    headers = {"Content-Type": "application/json"} if body is not None else {}
    data = json.dumps(body).encode() if body is not None else None
    with urlopen(Request(url, data=data, headers=headers), timeout=5) as response:
        return response.read()


def get_json(url, body=None):
    return json.loads(read(url, body))


def query(promql):
    response = get_json(PROM + "/api/v1/query?" + urlencode({"query": promql}))
    assert response["status"] == "success", "Prometheus query failed"
    return response["data"]["result"]


def main():
    state = get_json(APP + "/demo/state")
    assert state["fault_injected"] is False, "Existing demo fault active; leaving it unchanged"
    before_metrics = read(APP + "/metrics").decode()
    match = re.search(r'^checkout_requests_total\{status="ok"\}\s+(\S+)', before_metrics, re.M)
    # The labeled counter is absent until its first successful order.
    baseline = float(match.group(1)) if match else 0.0
    marker = "sentinel-checkpoint-1-8-" + uuid.uuid4().hex
    started_ns = time.time_ns()
    for index in range(COUNT):
        result = get_json(APP + "/checkout?" + urlencode({"sentinel_probe": marker}),
                          {"order_id": f"{marker}-{index}", "country": "US", "subtotal": 100})
        assert result["status"] == "accepted" and result["total"] == "107.00", "Healthy US checkout failed"
    print(f"Generated {COUNT} accepted US checkouts; waiting for fresh scrapes and matching logs.", flush=True)
    deadline = time.monotonic() + 60
    pending = "fresh telemetry"
    while time.monotonic() < deadline:
        values = query(SERIES)
        stamps = query("timestamp(" + SERIES + ")")
        targets = get_json(PROM + "/api/v1/targets")["data"]["activeTargets"]
        targets = [t for t in targets if t["labels"].get("job") == "checkout-demo"]
        params = urlencode({"query": '{service="checkout-service",environment="demo"} |= "' + marker + '"',
                            "start": str(started_ns), "end": str(time.time_ns()), "limit": 50, "direction": "forward"})
        logs = get_json(LOKI + "/loki/api/v1/query_range?" + params)
        assert logs["status"] == "success", "Loki query failed"
        streams = logs["data"]["result"]
        matching = [item for stream in streams for item in stream["values"]
                    if int(item[0]) >= started_ns and marker in item[1] and '" 200 ' in item[1]]
        metrics_ok = (len(values) == len(stamps) == 1
                      and float(values[0]["value"][1]) >= baseline + COUNT
                      and float(stamps[0]["value"][1]) >= started_ns / 1e9)
        target_ok = (len(targets) == 1 and targets[0]["health"] == "up" and not targets[0].get("lastError")
                     and datetime.fromisoformat(targets[0]["lastScrape"].replace("Z", "+00:00")).timestamp() >= started_ns / 1e9)
        logs_ok = len(matching) >= COUNT and all(
            s["stream"].get("service") == "checkout-service"
            and s["stream"].get("environment") == "demo"
            and s["stream"].get("job") == "checkout-demo" for s in streams)
        if metrics_ok and target_ok and logs_ok:
            print(json.dumps({"status": "passed", "marker": marker, "successful_orders": COUNT,
                              "counter_before": baseline, "prometheus_counter_after": float(values[0]["value"][1]),
                              "metric_sample_utc": datetime.fromtimestamp(float(stamps[0]["value"][1]), timezone.utc).isoformat(),
                              "target_last_scrape": targets[0]["lastScrape"], "matching_current_loki_lines": len(matching),
                              "stream_count": len(streams), "fault_injected": False}, indent=2))
            return 0
        pending = f"metrics_fresh={metrics_ok}, target_fresh={target_ok}, current_logs={logs_ok}"
        time.sleep(2)
    raise AssertionError("Telemetry deadline exceeded: " + pending)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        print(f"FAIL: {exc}")
        raise SystemExit(1)
    except (HTTPError, URLError, OSError):
        print("FAIL: local telemetry dependency unavailable; no error was converted into zero data.")
        raise SystemExit(1)
