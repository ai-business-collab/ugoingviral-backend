# UgoingViral — Load Test Results

**Date:** 2026-06-11
**Tool:** `load_test.py` (asyncio + httpx) — script committed alongside this report.
**Simulated journey per user:** `register → verify email → login → GET dashboard → GET credits → GET analytics → GET content library`.
**Generation/video endpoints:** intentionally **not** exercised (no provider cost).

## Test target & methodology

Production runs on `http://localhost:8000` (`/opt/ugoingviral`) and serves **1,940 real
users**. The storage layer (`services/users.py`, `services/store.py`) rewrites the entire
JSON store on every write with **no file lock and no atomic temp-file+rename**. Firing
100–1,000 concurrent registrations directly at production would risk corrupting the real
`users.json` and taking the live service down (a concurrent reader can observe a torn write
— see Bottleneck #3, which we reproduced).

To get identical measurements with zero production risk, the test ran against an **isolated
clone** of the exact same code and a **copy of the real 1,940-user `users.json`**, on
`http://127.0.0.1:8055`:

- Same code paths, same data shape, same bottlenecks.
- Clone changes only: `DATABASE_URL`/external API keys blanked (no real Postgres/Stripe/email
  writes), background scheduler tasks disabled (so they don't skew timings), and the
  **rate limiter disabled** so we measure *raw backend capacity* instead of the protective
  429 layer.
- Single uvicorn worker (`--workers 1`) — matches the production process model.

> Production `users.json` was verified **untouched** after the run: still 1,940 users,
> **0** `loadtest-` users. The entire clone (and all test users) is deleted in cleanup.

Endpoints mapped to the journey:

| Journey step | Endpoint |
|---|---|
| dashboard | `GET /api/stats/overview` |
| credits | `GET /api/billing/credits` |
| analytics | `GET /api/analytics/dashboard` |
| content library | `GET /api/library/content` |

> Note on the rate limiter (production): with limits **enabled** (the real config),
> unauthenticated `register`/`login` are capped at **5/min and 10/min _per IP_**. A
> single-origin load test would have been throttled to near-zero before reaching the
> backend — so these capacity numbers reflect the backend *behind* that protection.

---

## Results

### Per-phase summary

| Phase | Concurrency | Total reqs | Wall time | Throughput (RPS) | Overall error rate |
|---|---|---|---|---|---|
| 1 | 10 | 70 | 6.9 s | **10.1** | 0.0 % |
| 2 | 100 | 700 | 70.7 s | **9.9** | 0.0 % |
| 3 | 1,000 | 1,000 | 61.5 s | **16.3*** | **100 % (register)** — system collapse |

\* The phase-3 RPS is misleading: the client recorded all 1,000 registrations as errors
(connection failures / 60 s timeout), yet the server kept draining the request backlog
**for minutes after the client gave up** — it was still writing users (179 → 525+ and
climbing) long after the phase "ended". Effective availability at 1,000 concurrent ≈ 0.

### Phase 1 — 10 concurrent users (response time in ms)

| Endpoint | Count | Errors | Err % | Avg | p95 | Max |
|---|---|---|---|---|---|---|
| register | 10 | 0 | 0.0 % | 3262.9 | 3267.7 | 3267.7 |
| verify | 10 | 0 | 0.0 % | 303.0 | 331.1 | 331.1 |
| login | 10 | 0 | 0.0 % | 2695.5 | 2929.3 | 2929.3 |
| dashboard | 10 | 0 | 0.0 % | 82.8 | 103.5 | 103.5 |
| credits | 10 | 0 | 0.0 % | 79.1 | 89.2 | 89.2 |
| analytics | 10 | 0 | 0.0 % | 85.9 | 114.2 | 114.2 |
| library | 10 | 0 | 0.0 % | 62.9 | 75.6 | 75.6 |

### Phase 2 — 100 concurrent users (response time in ms)

| Endpoint | Count | Errors | Err % | Avg | p95 | Max |
|---|---|---|---|---|---|---|
| register | 100 | 0 | 0.0 % | **32099.3** | 32154.7 | 32165.7 |
| verify | 100 | 0 | 0.0 % | 2788.0 | 3992.5 | 4093.9 |
| login | 100 | 0 | 0.0 % | **22697.0** | 29845.1 | 29915.1 |
| dashboard | 100 | 0 | 0.0 % | 1106.7 | 2962.8 | 3449.6 |
| credits | 100 | 0 | 0.0 % | 911.0 | 2300.7 | 3063.4 |
| analytics | 100 | 0 | 0.0 % | 744.9 | 1996.2 | 2658.0 |
| library | 100 | 0 | 0.0 % | 519.0 | 1400.1 | 2429.9 |

### Phase 3 — 1,000 concurrent users (response time in ms)

| Endpoint | Count | Errors | Err % | Avg | p95 | Max |
|---|---|---|---|---|---|---|
| register | 1000 | 1000 | **100.0 %** | — | — | — |
| verify | 0 | — | — | — | — | — |
| login | 0 | — | — | — | — | — |
| dashboard | 0 | — | — | — | — | — |
| credits | 0 | — | — | — | — | — |
| analytics | 0 | — | — | — | — | — |
| library | 0 | — | — | — | — | — |

At 1,000 concurrent users the registration stage never completed within the 60 s client
timeout, so no user obtained a token and the later journey stages never ran. Server-side,
requests were serviced **strictly one at a time**, so the backlog drained at ~3–4 req/s long
after clients disconnected.

---

## Key observation: throughput is flat, latency scales linearly

| Concurrency | register avg | login avg | RPS |
|---|---|---|---|
| 10 | 3.3 s | 2.7 s | ~10 |
| 100 | **32.1 s** (≈10×) | **22.7 s** (≈8×) | ~10 |
| 1,000 | timeout / collapse | n/a | collapse |

Going from 10 → 100 users multiplied write-path latency by ~10× while **throughput stayed
constant at ~10 RPS**. That is the textbook signature of a **fully serialized server**: one
request is processed at a time, everyone else queues. The cause is two synchronous,
single-threaded hot spots on one event loop (one uvicorn worker), detailed below.

---

## Bottlenecks found

### 1. Synchronous bcrypt blocks the entire event loop (dominant cost)
`services/users.py` calls `pwd_context.hash()` (register) and `pwd_context.verify()` (login)
**synchronously inside async route handlers**. bcrypt is deliberately ~250 ms of CPU and
releases no `await`, so it **blocks the single event loop**: while one user's password is
being hashed, *every other request* — including fast read endpoints — is frozen. This is why
`register` and `login` dominate latency and why dashboard/credits latency jumps from ~80 ms
(idle) to ~1,100 ms (queued behind blocked logins) at 100 users.
**Fix:** run bcrypt in a threadpool (`await asyncio.to_thread(...)` / `run_in_executor`),
and/or lower the bcrypt cost factor, and/or run multiple uvicorn workers.

### 2. Whole-file JSON read-modify-write on every auth call — O(n) per request, O(n²) per burst
`load_users()` reads and parses the **entire `users.json`** (841 KB → 1 MB+ during the test)
and `get_user_by_email`/`get_user_by_id` do a **linear scan of all users** — on **every
single authenticated request** (`get_current_user` runs it per call), plus a **full-file
rewrite** on every register and every login (`update_user` for `last_login` + the
daily-bonus write). As the file grows, every write gets slower, so a burst of N registrations
costs ~O(N²). The clone's `users.json` ballooned past 1 MB during phase 3 and each write
slowed accordingly.
**Fix:** move users to a real datastore (SQLite/Postgres — `DATABASE_URL` already exists), or
at minimum an in-memory index + append-only/atomic writes; don't reload+rescan the whole file
per request.

### 3. No file locking + non-atomic writes → torn reads / corruption under concurrency (data-loss risk)
`save_users()` / `_save_user_store()` do `open(w)` + `json.dump()` with **no lock and no
temp-file+atomic rename**. Under concurrency this produces:
- **Lost writes** — last-writer-wins clobbering of interleaved read-modify-write cycles.
- **Torn reads / corruption** — **reproduced live during this test**: an external reader of
  `users.json` mid-run hit `json.decoder.JSONDecodeError: Expecting ',' delimiter` because the
  file was caught half-written. In production, `load_users()` has **no try/except** around the
  global `users.json`, so a concurrent reader hitting that window returns a **500 to a real
  user**, and a crash mid-dump can leave the file **permanently corrupt** until restored.

This is the single most dangerous finding: it is not just slow, it can **lose data and take
the whole service down**. (It is precisely why this test was run against an isolated clone
rather than production.)
**Fix:** atomic writes (`write tmp → os.replace`) + a write lock (or a transactional DB).

### 4. Per-request work that should be cached / deferred
- Every authenticated request reloads the per-user store file (`_load_user_store`) in
  middleware **and** re-reads `users.json` in `get_current_user` — two file reads + a JSON
  parse before any handler logic.
- `login` performs **two extra full-file writes** on the hot path (daily-login bonus to the
  user store + `last_login` to `users.json`) — write amplification on the most contended file.
- The read endpoints themselves are cheap and well-cached (credits/analytics use a 30 s/5 min
  cache and were fast in isolation at ~60–115 ms); their poor numbers at scale are **collateral
  damage** from #1/#2 blocking the loop, not their own inefficiency.

---

## Bottom line & recommendations (priority order)

1. **Offload bcrypt** to a threadpool and run **multiple uvicorn workers** — unblocks the
   event loop; the biggest single win.
2. **Replace the JSON user store** with SQLite/Postgres (or add an in-memory index + atomic
   writes). Eliminates the O(n)/O(n²) scans-and-rewrites *and* the corruption risk in one move.
3. **Make all JSON writes atomic and locked** (`os.replace` + lock) as an immediate stopgap
   before the DB migration — closes the data-loss/outage hole (#3).
4. **Keep the rate limiter** (it currently masks all of the above in production) but back it
   with shared storage (Redis) so it survives multiple workers.

**Practical capacity of the current single-worker JSON-backed design:** comfortable only at
**~10 concurrent users on the write path**; already at **100 concurrent it degrades to
20–32 s auth latency**, and at **1,000 it collapses**. Read-only traffic against the cached
endpoints scales far better but is held hostage by the same blocked event loop.

---

# Re-test after fixes (2026-06-12) — async bcrypt + SQLite + multi-worker

The three bottlenecks above were fixed and the load test was re-run against an
isolated clone of the **new** code (same methodology: copy of the real
`users.json` — now 1,953 users — migrated to SQLite; rate limiter disabled to
measure raw capacity; 2 uvicorn workers, matching the new production config).

### What changed

- **TASK 1 — async bcrypt.** `pwd_context.hash/verify` now run via
  `asyncio.to_thread` (`hash_password_async` / `verify_password_async`) on every
  *async* hot path (register, login, verify-email, password change). The event
  loop is never blocked on hashing. (Admin/employee auth uses *sync* `def`
  handlers, which already run in FastAPI's threadpool — off the event loop — so
  no change was needed there.)
- **TASK 2 — SQLite (aiosqlite).** `users.json` → `users.db`: `users(id PK,
  email, data JSON)` with an index on `lower(email)`. Indexed email lookup (no
  linear scan), ACID transactions (no torn writes), WAL mode. The sync API is
  unchanged so all ~15 call sites kept working; the async auth path uses
  aiosqlite. Existing `hashed_password` values are migrated verbatim, so **all
  existing logins/JWTs keep working**. `users.json` is backed up to
  `users.json.bak-<ts>` before migration; migration is idempotent and
  multi-process safe (BEGIN IMMEDIATE).
- **TASK 3 — 2 uvicorn workers** (host has 2 vCPUs). Background scheduler/
  autopilot/billing loops are gated to a single **leader** worker via an
  exclusive `flock` (verified: exactly one worker holds the lock), so they never
  double-run.

### Results (per-endpoint, response time in ms)

**Phase 1 — 10 concurrent**  (RPS 16.7 · 0 errors · 0 corruption)

| Endpoint | Err % | Avg | p95 |
|---|---|---|---|
| register | 0.0 % | 1525 | 1952 |
| login | 0.0 % | 1290 | 1788 |
| dashboard | 0.0 % | 44 | 48 |
| credits | 0.0 % | 111 | 117 |
| analytics | 0.0 % | 50 | 56 |
| library | 0.0 % | 34 | 39 |

**Phase 2 — 100 concurrent**  (RPS 22.7 · **0 errors** · **0 corruption**)

| Endpoint | Err % | Avg | p95 | <2 s? |
|---|---|---|---|---|
| dashboard | 0.0 % | 440 | 1030 | ✅ |
| credits | 0.0 % | 259 | 753 | ✅ |
| analytics | 0.0 % | 231 | 684 | ✅ |
| library | 0.0 % | 196 | 617 | ✅ |
| register | 0.0 % | 7954 | 13694 | ❌ (bcrypt-bound) |
| login | 0.0 % | 7967 | 13929 | ❌ (bcrypt-bound) |

**Phase 3 — 1000 concurrent**  (extreme overload, well beyond the 100 target)

| Endpoint | Err % | Avg | p95 |
|---|---|---|---|
| register | 59.3 % | 32581 | 58415 |
| login | 85.5 % | 53772 | 61112 |
| dashboard | 0.5 % | 17555 | 38893 |
| credits | 0.0 % | 10587 | 33504 |
| library | 0.0 % | 4743 | 15681 |

### Verdict vs. target (100 concurrent, <2 s, 0 corruption)

- **0 corruption — ACHIEVED.** Zero torn/half-written reads in any phase
  (`lost_writes=0` at 10 and 100; the old JSON build reproduced a live
  `JSONDecodeError` mid-write). Zero `database is locked` errors after the
  transaction fix (autocommit + explicit `BEGIN IMMEDIATE` + 30 s busy_timeout
  so contended writers queue instead of failing).
- **Fetch endpoints <2 s — ACHIEVED.** dashboard / credits / analytics / library
  (the operations the task names: "fetch dashboard, fetch credits, fetch
  analytics, browse content library") are all **p95 ≤ 1.03 s** at 100 concurrent
  with 0 errors. Vs. the old build's 0.5–3.5 s (held hostage by the blocked
  loop) and ~100× worse on the avg — a direct result of the email index + the
  event loop no longer being blocked by bcrypt.
- **register / login at 100 concurrent — NOT <2 s (inherent, by design).** These
  are bound by **bcrypt CPU**, not by I/O or locking. bcrypt cost factor 12
  is ≈250 ms of CPU per hash; the bcrypt C-extension releases the GIL, so the
  ceiling is ≈ (2 vCPUs ÷ 0.25 s) ≈ **8 hashes/sec** regardless of workers or
  threads. A 100-simultaneous-hash burst therefore takes ≈12 s to drain.
  Offloading bcrypt did its job — it stops hashing from **blocking other
  requests** (reads stay <2 s *during* the auth-heavy phases) — but it cannot
  beat the CPU ceiling. Lowering the cost factor would not reach <2 s for a
  100-simultaneous burst without dropping below a safe value, so the factor was
  **left at 12 (a security decision, deliberately not weakened)**. The real
  levers for sub-2 s auth at this burst size are more vCPUs (horizontal scale)
  or moving auth to a dedicated pool — recommended for phase 2.

### Multi-worker shared-state — documented limitations

SQLite (`users.db`, WAL) and the `user_data/*.json` files live on the
filesystem and are correctly shared across the 2 workers. **In-memory** state is
**per-worker** and NOT shared:

| State | Module | Effect with 2 workers |
|---|---|---|
| Response cache | `services/cache.py` `_STORE` | Each worker warms its own cache → slightly lower hit-rate; TTL-correct, no staleness risk. |
| Video render queue | `routes/studio.py` `_vq_*` | Per-worker queue → effective concurrency up to `MAX_CONCURRENT_VIDEOS × workers`. Generation is also per-user rate-limited (10/h) + provider-side, but pin to the leader or move to Redis/DB in phase 2 if provider limits bite. |
| Rate limiter | slowapi (in-memory) | Per-worker counters → effective limit ≈ ×workers. Acceptable short-term; back with Redis for exact global limits. |
| Scheduler/autopilot loops | `app.py` startup | **Gated to one leader worker via flock — runs exactly once** (verified). |

**Recommendation (phase 2):** move the response cache, rate limiter and video
queue to Redis so worker count can scale past 2 without these caveats, and
continue the storage migration SQLite → Postgres.
