#!/usr/bin/env python3
"""
UgoingViral load test (asyncio + httpx).

Simulates a per-user journey at three concurrency levels (10 / 100 / 1000):
    register  ->  verify email  ->  login  ->  GET dashboard / credits /
    analytics / content library.

Video / generation endpoints are deliberately NOT exercised (no provider cost).

Run against an isolated CLONE of the production app (identical code + a copy of
the real 1940-user users.json) so the real production store is never touched.
The rate limiter is disabled in the clone so we measure raw backend capacity
rather than the protective 429 layer.

Per phase it reports, per endpoint: count, errors, avg / p95 / max latency, and
the phase-wide requests-per-second. It also measures the "lost write" rate on
the lock-free users.json write path.
"""
import asyncio
import json
import os
import statistics
import time
import uuid

import httpx

BASE_URL   = os.getenv("LT_BASE", "http://127.0.0.1:8055")
# Users now live in SQLite (post-migration). Verification codes are read straight
# from the DB. (Falls back to the legacy users.json path if no DB is given.)
USERS_DB   = os.getenv("LT_USERS_DB", "/tmp/ugv_loadtest/users.db")
USERS_JSON = os.getenv("LT_USERS_JSON", "/tmp/ugv_loadtest/users.json")
PASSWORD   = "loadtestPass123"
PHASES     = [10, 100, 1000]
EMAIL_PREFIX = "loadtest-"          # used for cleanup / accounting

# endpoint label -> (method, path, needs_auth)
READ_ENDPOINTS = [
    ("dashboard",  "GET", "/api/stats/overview"),
    ("credits",    "GET", "/api/billing/credits"),
    ("analytics",  "GET", "/api/analytics/dashboard"),
    ("library",    "GET", "/api/library/content"),
]


class Metric:
    """Latency samples + error count for one logical endpoint within a phase."""
    def __init__(self):
        self.lat = []      # successful-call latencies (ms)
        self.errors = 0
        self.count = 0

    def add(self, ms, ok):
        self.count += 1
        if ok:
            self.lat.append(ms)
        else:
            self.errors += 1

    def summary(self):
        lat = self.lat
        if lat:
            s = sorted(lat)
            p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
            avg = statistics.mean(s)
            mx = max(s)
        else:
            p95 = avg = mx = 0.0
        return {
            "count": self.count,
            "errors": self.errors,
            "error_rate": (self.errors / self.count * 100.0) if self.count else 0.0,
            "avg_ms": avg,
            "p95_ms": p95,
            "max_ms": mx,
        }


async def timed(client, method, path, *, headers=None, json_body=None):
    """Issue one request; return (latency_ms, ok, json_or_None)."""
    t0 = time.perf_counter()
    try:
        r = await client.request(method, BASE_URL + path, headers=headers, json=json_body)
        ms = (time.perf_counter() - t0) * 1000.0
        ok = 200 <= r.status_code < 300
        body = None
        if ok:
            try:
                body = r.json()
            except Exception:
                body = None
        return ms, ok, body
    except Exception:
        return (time.perf_counter() - t0) * 1000.0, False, None


async def bounded_gather(coros, limit):
    """Run coros with at most `limit` in flight (== the phase's concurrency)."""
    sem = asyncio.Semaphore(limit)

    async def run(c):
        async with sem:
            return await c
    return await asyncio.gather(*(run(c) for c in coros))


def load_codes(emails):
    """Map each registered test email -> (user_id, verification_code).

    Reads from the SQLite users.db (post-migration) when present, else falls
    back to the legacy users.json. Returns ({}, error_string) if the store is
    unreadable — which, with SQLite's atomic writes, should no longer happen
    under concurrency (the old JSON path could be caught mid torn-write)."""
    wanted = set(emails)
    out = {}
    if USERS_DB and os.path.exists(USERS_DB):
        import sqlite3
        try:
            conn = sqlite3.connect(USERS_DB, timeout=10.0)
            conn.execute("PRAGMA busy_timeout=10000")
            for (data_str,) in conn.execute("SELECT data FROM users"):
                u = json.loads(data_str)
                em = u.get("email", "")
                if em in wanted:
                    out[em] = (u.get("id"), u.get("verification_code", ""))
            conn.close()
            return out, None
        except Exception as e:
            return {}, f"{type(e).__name__}: {e}"
    try:
        with open(USERS_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {}, f"{type(e).__name__}: {e}"
    for u in data.get("users", []):
        em = u.get("email", "")
        if em in wanted:
            out[em] = (u.get("id"), u.get("verification_code", ""))
    return out, None


async def run_phase(n):
    print(f"\n{'='*70}\nPHASE: {n} concurrent users\n{'='*70}")
    run_id = uuid.uuid4().hex[:6]
    emails = [f"{EMAIL_PREFIX}p{n}-{run_id}-{i}@loadtest.local" for i in range(n)]

    metrics = {k: Metric() for k in ("register", "verify", "login",
                                     "dashboard", "credits", "analytics", "library")}
    phase_start = time.perf_counter()
    total_requests = 0

    limits = httpx.Limits(max_connections=n + 50, max_keepalive_connections=n + 50)
    timeout = httpx.Timeout(60.0)
    async with httpx.AsyncClient(limits=limits, timeout=timeout) as client:
        # ---- Stage 1: REGISTER (concurrent, writes to global users.json) ----
        async def do_register(em):
            ms, ok, _ = await timed(client, "POST", "/api/auth/register",
                                    json_body={"email": em, "password": PASSWORD, "name": "LT"})
            metrics["register"].add(ms, ok)
            return em, ok
        reg = await bounded_gather([do_register(e) for e in emails], n)
        total_requests += len(reg)
        registered_ok = [e for e, ok in reg if ok]

        # Lost-write accounting: how many ok-registrations actually persisted?
        code_map, read_err = load_codes(emails)
        persisted = len(code_map)

        # ---- Stage 2: VERIFY EMAIL (concurrent) -> tokens ----
        tokens = {}
        async def do_verify(em):
            uid, code = code_map.get(em, (None, ""))
            if not code:
                metrics["verify"].add(0.0, False)
                return
            ms, ok, body = await timed(client, "POST", "/api/auth/verify_email",
                                       json_body={"email": em, "code": code})
            metrics["verify"].add(ms, ok)
            if ok and body and body.get("token"):
                tokens[em] = body["token"]
        await bounded_gather([do_verify(e) for e in registered_ok], n)
        total_requests += len(registered_ok)

        # ---- Stage 3: LOGIN (concurrent, full path incl. daily-bonus write) ----
        async def do_login(em):
            ms, ok, body = await timed(client, "POST", "/api/auth/login",
                                       json_body={"email": em, "password": PASSWORD})
            metrics["login"].add(ms, ok)
            if ok and body and body.get("token"):
                tokens[em] = body["token"]
        await bounded_gather([do_login(e) for e in list(tokens.keys()) or registered_ok], n)
        total_requests += len(list(tokens.keys()) or registered_ok)

        # ---- Stage 4: READ JOURNEY (concurrent: 4 GETs per user) ----
        async def do_reads(em):
            tok = tokens.get(em)
            if not tok:
                for label, *_ in READ_ENDPOINTS:
                    metrics[label].add(0.0, False)
                return
            hdr = {"Authorization": f"Bearer {tok}"}
            for label, method, path in READ_ENDPOINTS:
                ms, ok, _ = await timed(client, method, path, headers=hdr)
                metrics[label].add(ms, ok)
        read_users = registered_ok
        await bounded_gather([do_reads(e) for e in read_users], n)
        total_requests += len(read_users) * len(READ_ENDPOINTS)

    phase_secs = time.perf_counter() - phase_start
    rps = total_requests / phase_secs if phase_secs else 0.0

    result = {
        "concurrency": n,
        "wall_seconds": round(phase_secs, 2),
        "total_requests": total_requests,
        "rps": round(rps, 1),
        "registered_ok": len(registered_ok),
        "persisted_in_file": persisted,
        "lost_writes": len(registered_ok) - persisted,
        "users_file_read_error": read_err,
        "endpoints": {k: m.summary() for k, m in metrics.items()},
    }
    # console summary
    print(f"  wall={phase_secs:.1f}s  total_req={total_requests}  RPS={rps:.1f}")
    print(f"  registered_ok={len(registered_ok)}  persisted={persisted}  "
          f"lost_writes={result['lost_writes']}  file_read_error={read_err}")
    print(f"  {'endpoint':<11} {'count':>6} {'err':>5} {'err%':>6} {'avg':>8} {'p95':>8} {'max':>9}")
    for k in ("register", "verify", "login", "dashboard", "credits", "analytics", "library"):
        s = result["endpoints"][k]
        print(f"  {k:<11} {s['count']:>6} {s['errors']:>5} {s['error_rate']:>5.1f}% "
              f"{s['avg_ms']:>7.1f} {s['p95_ms']:>7.1f} {s['max_ms']:>8.1f}")
    return result


async def main():
    print(f"Target: {BASE_URL}  (isolated clone)")
    h = httpx.get(BASE_URL + "/health", timeout=5)
    print("health:", h.json())
    results = []
    for n in PHASES:
        results.append(await run_phase(n))
        await asyncio.sleep(2)  # brief settle between phases
    with open("/tmp/ugv_loadtest_results.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nWrote /tmp/ugv_loadtest_results.json")


if __name__ == "__main__":
    asyncio.run(main())
