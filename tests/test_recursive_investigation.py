"""Offline tests for the recursive dependency-chase harness (no LLM, no network)."""

from coded_tools.sentinel.recursive_investigation import run_investigation_chain
from coded_tools.sentinel.recursive_investigation import summarize_chain
from coded_tools.sentinel.service_dependencies import upstream_of


def _sly_data(verdict, service_name, ticket_number):
    return {
        "sentinel_ticket_number": ticket_number,
        "sentinel_verified": {"correlation": {"verdict": verdict, "service_name": service_name,
                                               "reasoning": f"{service_name} is {verdict}"}},
    }


def test_upstream_of_known_and_unknown_service():
    assert upstream_of("checkout-service") == ["inventory-service"]
    assert upstream_of("auth-service") == []
    assert upstream_of("not-a-real-service") == []


def test_supported_first_hop_stops_immediately():
    calls = []

    def investigate_one(service_name):
        calls.append(service_name)
        return _sly_data("SUPPORTED", service_name, "INC1"), {"text": "ok"}

    _, _, chain = run_investigation_chain(investigate_one, "checkout-service")
    assert calls == ["checkout-service"]
    assert [hop["service_name"] for hop in chain] == ["checkout-service"]
    assert chain[0]["verdict"] == "SUPPORTED"


def test_not_supported_chases_dependency():
    calls = []

    def investigate_one(service_name):
        calls.append(service_name)
        ticket = f"INC{len(calls)}"
        verdict = "SUPPORTED" if service_name == "inventory-service" else "NOT_SUPPORTED"
        return _sly_data(verdict, service_name, ticket), {"text": "ok"}

    _, _, chain = run_investigation_chain(investigate_one, "checkout-service")
    assert calls == ["checkout-service", "inventory-service"]
    assert [hop["verdict"] for hop in chain] == ["NOT_SUPPORTED", "SUPPORTED"]
    assert "checkout-service (NOT_SUPPORTED) -> inventory-service (SUPPORTED)" in summarize_chain(chain)


def test_depth_cap_stops_recursion():
    calls = []

    def investigate_one(service_name):
        calls.append(service_name)
        return _sly_data("NOT_SUPPORTED", service_name, f"INC{len(calls)}"), {"text": "ok"}

    _, _, chain = run_investigation_chain(investigate_one, "checkout-service", max_depth=1)
    # depth=0 investigates checkout-service, depth=1 investigates inventory-service, then stops at the cap.
    assert calls == ["checkout-service", "inventory-service"]
    assert len(chain) == 2


def test_cycle_is_not_infinite():
    calls = []

    def investigate_one(service_name):
        calls.append(service_name)
        return _sly_data("NOT_SUPPORTED", service_name, f"INC{len(calls)}"), {"text": "ok"}

    # Even if a dependency map had a cycle, upstream_of() for a leaf service returns [], so the
    # walk below (checkout -> inventory -> notification -> []) must terminate on its own.
    _, _, chain = run_investigation_chain(investigate_one, "checkout-service", max_depth=10)
    assert len(calls) == len(set(calls))  # no service investigated twice
    assert len(chain) <= 10


def test_no_known_dependency_stops_after_one_hop():
    calls = []

    def investigate_one(service_name):
        calls.append(service_name)
        return _sly_data("NOT_SUPPORTED", service_name, "INC1"), {"text": "ok"}

    _, _, chain = run_investigation_chain(investigate_one, "auth-service")
    assert calls == ["auth-service"]
    assert len(chain) == 1


def test_summarize_chain_empty_for_single_hop():
    chain = [{"service_name": "auth-service", "verdict": "NOT_SUPPORTED"}]
    assert summarize_chain(chain) == ""
