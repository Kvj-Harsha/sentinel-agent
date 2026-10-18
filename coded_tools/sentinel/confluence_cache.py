"""
Small bounded LRU+TTL cache in front of Confluence runbook lookups.

Confluence search/page reads are read-only evidence (never ticket or PR state), so a stale or
evicted entry can never cause an incorrect ticket/PR decision -- it only costs one extra network
round trip. That makes it a safe place to demonstrate a real eviction policy: a bounded
``OrderedDict`` evicts the least-recently-used entry once ``maxsize`` is exceeded, and entries
older than ``ttl_seconds`` are treated as misses so a runbook edited in Confluence is picked up
again within that window.
"""

import time
from collections import OrderedDict
from threading import Lock
from typing import Any
from typing import Callable
from typing import Dict
from typing import Tuple


class LRUTTLCache:
    """A tiny thread-safe LRU cache with a time-to-live, and real hit/miss/eviction counters."""

    def __init__(self, maxsize: int = 32, ttl_seconds: int = 300) -> None:
        self.maxsize = maxsize
        self.ttl_seconds = ttl_seconds
        self._data: "OrderedDict[str, Tuple[float, Any]]" = OrderedDict()
        self._lock = Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get_or_set(self, key: str, compute: Callable[[], Any]) -> Any:
        """Returns the cached value for ``key``, else calls ``compute()``, caches and returns it."""
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is not None:
                stored_at, value = entry
                if now - stored_at <= self.ttl_seconds:
                    self._data.move_to_end(key)
                    self.hits += 1
                    return value
                del self._data[key]  # expired -- treat as a miss below
        value = compute()  # outside the lock: a slow network call must not block other keys
        with self._lock:
            self.misses += 1
            self._data[key] = (now, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)  # evict least-recently-used
                self.evictions += 1
        return value

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"hits": self.hits, "misses": self.misses, "evictions": self.evictions, "size": len(self._data)}

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_search_cache = LRUTTLCache(maxsize=32, ttl_seconds=300)
_page_cache = LRUTTLCache(maxsize=32, ttl_seconds=300)


def cached_search(confluence, query: str):
    """Cached wrapper for ``ConfluenceRunbooks.search``, keyed by the normalized query text."""
    return _search_cache.get_or_set(query.strip().lower(), lambda: confluence.search(query))


def cached_get_page(confluence, page_id: str):
    """Cached wrapper for ``ConfluenceRunbooks.get_page``, keyed by page id."""
    return _page_cache.get_or_set(str(page_id), lambda: confluence.get_page(page_id))


def cache_stats() -> Dict[str, Any]:
    """:return: hit/miss/eviction/size counters for both caches, for the UI's perf panel."""
    return {"search": _search_cache.stats(), "page": _page_cache.stats()}


def reset_caches() -> None:
    """Test-only helper: clears both caches and their counters."""
    global _search_cache, _page_cache
    _search_cache = LRUTTLCache(maxsize=_search_cache.maxsize, ttl_seconds=_search_cache.ttl_seconds)
    _page_cache = LRUTTLCache(maxsize=_page_cache.maxsize, ttl_seconds=_page_cache.ttl_seconds)
