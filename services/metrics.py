"""
In-process runtime metrics for /api/health/detailed:
  - API response times (ring buffer of the last 100 requests)
  - active users / sessions (distinct identities seen in a recent window)
  - process + system memory usage (read from /proc, no psutil dependency)

All counters are in-memory and reset on restart — they describe the live
process, which is exactly what a load test wants to watch.
"""
import time
from collections import deque
from threading import Lock

_LOCK = Lock()
_REQ_MS: deque = deque(maxlen=100)     # durations (ms) of the last 100 requests
_ACTIVE: dict = {}                     # identity -> last-seen epoch seconds
_ACTIVE_CAP = 10000                    # prune guard


def record_request(duration_ms: float, identity: str = "") -> None:
    """Record one request's duration and (optionally) the caller identity."""
    now = time.time()
    with _LOCK:
        _REQ_MS.append(duration_ms)
        if identity:
            _ACTIVE[identity] = now
            if len(_ACTIVE) > _ACTIVE_CAP:
                cutoff = now - 900
                for k in [k for k, v in _ACTIVE.items() if v < cutoff]:
                    _ACTIVE.pop(k, None)


def response_time_stats() -> dict:
    with _LOCK:
        times = list(_REQ_MS)
    if not times:
        return {"sample_size": 0, "avg_ms": 0, "min_ms": 0, "max_ms": 0, "p95_ms": 0}
    ordered = sorted(times)
    n = len(ordered)
    p95 = ordered[min(n - 1, int(round(n * 0.95)) - 1 if n > 1 else 0)]
    return {
        "sample_size": n,
        "avg_ms": round(sum(ordered) / n, 2),
        "min_ms": round(ordered[0], 2),
        "max_ms": round(ordered[-1], 2),
        "p95_ms": round(p95, 2),
    }


def active_stats(window_s: int = 300) -> dict:
    """Distinct identities seen in the last window_s seconds.
    'user:' identities are authenticated users; everything else is per-IP."""
    now = time.time()
    with _LOCK:
        items = list(_ACTIVE.items())
    users = set()
    sessions = 0
    for ident, ts in items:
        if now - ts <= window_s:
            sessions += 1
            if ident.startswith("user:"):
                users.add(ident)
    return {
        "active_sessions": sessions,
        "active_users": len(users),
        "window_seconds": window_s,
    }


def memory_usage() -> dict:
    """Process RSS + system memory, parsed from /proc (Linux)."""
    info: dict = {}
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    info["process_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
                elif line.startswith("VmHWM:"):
                    info["process_peak_rss_mb"] = round(int(line.split()[1]) / 1024, 1)
    except Exception:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    mem[parts[0].strip()] = int(parts[1].strip().split()[0])  # kB
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", 0)
        if total:
            info["system_total_mb"] = round(total / 1024, 1)
            info["system_available_mb"] = round(avail / 1024, 1)
            info["system_used_pct"] = round((total - avail) / total * 100, 1)
    except Exception:
        pass
    return info
