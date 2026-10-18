"""Offline tests for the Confluence LRU+TTL cache (no network)."""

import time

from coded_tools.sentinel.confluence_cache import LRUTTLCache


def test_hit_on_repeated_key():
    cache = LRUTTLCache(maxsize=4, ttl_seconds=60)
    calls = []

    def compute():
        calls.append(1)
        return "value"

    assert cache.get_or_set("a", compute) == "value"
    assert cache.get_or_set("a", compute) == "value"
    assert len(calls) == 1
    assert cache.stats() == {"hits": 1, "misses": 1, "evictions": 0, "size": 1}


def test_eviction_past_maxsize_drops_least_recently_used():
    cache = LRUTTLCache(maxsize=2, ttl_seconds=60)
    cache.get_or_set("a", lambda: "a-value")
    cache.get_or_set("b", lambda: "b-value")
    cache.get_or_set("c", lambda: "c-value")  # evicts "a" (least recently used)

    stats = cache.stats()
    assert stats["evictions"] == 1
    assert stats["size"] == 2

    calls = []

    def compute_a():
        calls.append("a")
        return "a-value-2"

    assert cache.get_or_set("a", compute_a) == "a-value-2"
    assert calls == ["a"]  # "a" really was evicted -- recomputed, not served from cache


def test_recently_used_key_survives_eviction():
    cache = LRUTTLCache(maxsize=2, ttl_seconds=60)
    cache.get_or_set("a", lambda: "a-value")
    cache.get_or_set("b", lambda: "b-value")
    cache.get_or_set("a", lambda: "a-value")  # touch "a" so "b" becomes least-recently-used
    cache.get_or_set("c", lambda: "c-value")  # evicts "b", not "a"

    calls = []

    def compute_a():
        calls.append("a")
        return "a-value-2"

    assert cache.get_or_set("a", compute_a) == "a-value"  # still cached
    assert calls == []


def test_ttl_expiry_forces_recompute():
    cache = LRUTTLCache(maxsize=4, ttl_seconds=0)
    cache.get_or_set("a", lambda: "a-value")
    time.sleep(0.01)
    calls = []

    def compute():
        calls.append(1)
        return "a-value-2"

    assert cache.get_or_set("a", compute) == "a-value-2"
    assert calls == [1]


def test_failed_compute_is_not_cached():
    cache = LRUTTLCache(maxsize=4, ttl_seconds=60)

    def failing():
        raise RuntimeError("boom")

    try:
        cache.get_or_set("a", failing)
    except RuntimeError:
        pass
    assert cache.stats() == {"hits": 0, "misses": 0, "evictions": 0, "size": 0}
