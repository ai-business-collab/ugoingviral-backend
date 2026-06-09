"""
Tiny in-memory TTL cache (no Redis) for response caching of expensive endpoints.

Used by the cached_endpoint() helper in routes — keyed per (endpoint, user) so
one user's cached payload never leaks to another. Single-process only; entries
evaporate on restart, which is fine for short-lived response caches.
"""
import time
from threading import Lock

_LOCK = Lock()
_STORE: dict = {}          # key -> {"expires": ts, "value": <json-able>}
_MAX_ENTRIES = 5000        # safety cap so a flood of distinct keys can't grow unbounded

# Lightweight hit/miss counters for the /api/health/detailed view.
_HITS = 0
_MISSES = 0


def cache_get(key: str):
    """Return (hit: bool, value). Misses and expired entries return (False, None)."""
    global _HITS, _MISSES
    now = time.time()
    with _LOCK:
        entry = _STORE.get(key)
        if entry and entry["expires"] > now:
            _HITS += 1
            return True, entry["value"]
        if entry:                       # expired — drop it
            _STORE.pop(key, None)
        _MISSES += 1
        return False, None


def cache_set(key: str, value, ttl: int) -> None:
    """Store value under key for ttl seconds."""
    now = time.time()
    with _LOCK:
        if len(_STORE) >= _MAX_ENTRIES:
            # Drop expired first; if still full, drop the soonest-to-expire 10%.
            for k in [k for k, v in _STORE.items() if v["expires"] <= now]:
                _STORE.pop(k, None)
            if len(_STORE) >= _MAX_ENTRIES:
                for k in sorted(_STORE, key=lambda k: _STORE[k]["expires"])[: max(1, _MAX_ENTRIES // 10)]:
                    _STORE.pop(k, None)
        _STORE[key] = {"expires": now + ttl, "value": value}


def cache_invalidate(prefix: str) -> int:
    """Drop every entry whose key starts with prefix. Returns count removed."""
    with _LOCK:
        keys = [k for k in _STORE if k.startswith(prefix)]
        for k in keys:
            _STORE.pop(k, None)
        return len(keys)


def user_cache_key(prefix: str) -> str:
    """Build a per-user cache key from the active request's store context so one
    user's cached payload can never be served to another (sub-accounts included)."""
    try:
        from services.store import _uid_ctx
        uid = _uid_ctx.get(None)
    except Exception:
        uid = None
    return f"{prefix}:{uid or 'anon'}"


def cache_stats() -> dict:
    now = time.time()
    with _LOCK:
        active = sum(1 for v in _STORE.values() if v["expires"] > now)
        total = len(_STORE)
    reqs = _HITS + _MISSES
    return {
        "entries": total,
        "active_entries": active,
        "hits": _HITS,
        "misses": _MISSES,
        "hit_rate": round(_HITS / reqs, 3) if reqs else 0.0,
    }
