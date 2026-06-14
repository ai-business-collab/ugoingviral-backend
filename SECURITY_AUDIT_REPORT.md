# UgoingViral — Security Audit Report

**Date:** 2026-06-14
**Target:** `/root/ugoingviral-backend` (FastAPI, 2 uvicorn workers behind nginx, HTTPS)
**Method:** Live attacks against the running server (`localhost:8000`) + source review. Every
result below was produced by a real request, not assumption. The harness used is
`security_audit_test.py` (committed alongside this report).

## Verdict

The core security property — **per-user data isolation** — is **solid**. Data access is
enforced by a request-scoped `ContextVar` store (`services/store.py`) that is keyed off the
authenticated JWT in middleware, so every request can only ever read/write the store belonging
to *its own* token. No endpoint accepts another user's id to look up data (the admin endpoints
are role-gated with a separate signing key). IDOR, SQLi, and admin-escalation attacks all
**failed to breach**.

Three real weaknesses were found and **fixed immediately**:

1. **Expensive AI/content endpoints were reachable with no authentication** (anonymous
   cost-abuse / DoS). — FIXED
2. **No server-side logout / token revocation** — a copied token stayed valid for its full
   30-day life after "logout". — FIXED
3. **JWT signing key could silently fall back to a public hardcoded default** if `.env`
   failed to load. — FIXED (now fails closed)

Plus header hardening (HSTS/CSP), and removal of an unauthenticated internal referral endpoint.

One broader item is logged as **larger work** (defense-in-depth, not a breach): many
non-sensitive endpoints return `200` with empty data instead of `401` when unauthenticated.

---

## TASK 1 — Attack results

| # | Attack | Result | Evidence |
|---|--------|--------|----------|
| a | **IDOR — read another user's data** | ✅ PASS | User A's `/api/posts/scheduled` and `/api/settings` returned only A's data; B's `POST_BY_B` / `SECRET_B_ig` never appeared. |
| a | **IDOR — delete/reschedule B's post by id** | ✅ PASS | A's `DELETE /api/posts/{B_pid}` and `PATCH /api/posts/{B_pid}/reschedule` did not touch B's post (B's post survived; reschedule → `404`). The `{id}` only indexes A's own ContextVar store. |
| b | **SQL injection on login** (`admin'--`, `' OR 1=1--`, `'; DROP TABLE users;--`, `UNION SELECT`, …) | ✅ PASS | All payloads returned `400` (WAF) or `401` (invalid) — never a token. `users` table intact (8 rows) after the `DROP TABLE` payload. Queries are parameterized (SQLite `?` binds) and a global `InputValidationMiddleware` rejects SQLi/XSS before routing. |
| c | **Reuse token after logout** | ✅ PASS *(after fix)* | Before fix: no logout endpoint existed and the old token still worked (`/api/auth/me` → `200`). After fix: `POST /api/auth/logout` revokes the token; reusing it → `401` on `/api/auth/me` **and** on `/api/content/generate`. |
| d | **No token / invalid token** | ✅ PASS (no leak) | No-token and `Bearer not.a.real.token` requests **never leaked another user's data**. `/api/auth/me` correctly returns `401` for both. See "larger work" for endpoints that return `200`-empty instead of `401`. |
| d | **Unauthenticated expensive endpoints** | ✅ PASS *(after fix)* | `/api/content/generate` and all content/AI/video/voice/upload endpoints now return `401` without a token (was reaching the paid AI handler). |
| e | **Admin access by a normal user** | ✅ PASS | Normal-user token against `/api/admin/users`, `/api/admin/affiliates`, `/api/admin/employees`, `/api/admin/templates`, `/api/health/detailed` → all `401/403`. A **forged `role=owner` token signed with the user JWT secret** was rejected (`401`) — admin uses a *separate* signing key + role claim. |
| f | **User isolation across data endpoints** | ✅ PASS | Confirmed via (a). Every data endpoint resolves data from the JWT-derived ContextVar store or `_load_user_store(current_user["id"])`; none accept a caller-supplied user id. |

**Notable false positives ruled out by live testing** (a pre-scan flagged these; verification
showed the ContextVar store makes them safe): `posts.py` `{pid}` handlers, `content_library`
`{item_id}` handlers, and `analytics/update_performance` `post_id` all operate **only within the
authenticated user's own store**, so a foreign id simply isn't found.

---

## TASK 2 — Protections verified

| Control | Status | Detail |
|---------|--------|--------|
| **Rate limiting** | ✅ Active (one caveat) | Login `10/min` (verified: 10×`401` → 6×`429`) + nginx `limit_req` zone; register `5/min`; video render `10/hr/user`; global default `60/min` keyed **per-user when a JWT is present, per-IP otherwise** (`_rate_key`). Brute-force IP block after 10 failed logins / 5 min → 15 min block (file-based, **shared across workers**). **Caveat:** the slowapi 60/min counter is in-memory and therefore **per-worker** (≈2× effective limit with 2 workers, and would not be shared across hosts). The auth-critical limits (login/register/IP-block) are reliable. *Recommendation: move to Redis-backed storage before scaling out.* |
| **Input validation / sanitization** | ✅ Active | `InputValidationMiddleware` scans query strings (all methods) and JSON bodies for SQLi/XSS and rejects with `400`. Verified: `<script>…` body → `400`; `?q=1 OR 1=1--` → `400`. Server-side, runs before every route. |
| **Security headers** | ✅ Now complete | Already present: `X-Content-Type-Options`, `X-Frame-Options: DENY`, `X-XSS-Protection`, `Referrer-Policy`. **Added:** `Strict-Transport-Security` (1y + subdomains), a conservative `Content-Security-Policy` (`frame-ancestors 'none'; object-src 'none'; base-uri 'self'`), and `Permissions-Policy`. Verified live on `/health`. |
| **No secrets exposed in frontend / API** | ✅ PASS | No API keys/secrets found in `frontend/`. `GET /api/settings` **masks** all credential fields (tokens/passwords/AI keys → first 4 chars + `••••`). nginx denies `/.env`, `*.py`, `/user_data/`, `users.json`. |
| **No source maps in production** | ✅ PASS | No `*.map` files exist under `frontend/`/`static/`; no `sourceMappingURL` references. The frontend is shipped as-is (not minified+mapped), so no original source is exposed via maps. |

---

## TASK 3 — Server health + runaway monitoring (added to QA agent)

The QA agent (`qa_agent.py`, cron `0 */6 * * *`) now runs `check_server_health()` at the end of
every run and **alerts via the same Telegram report channel** — and the alert now fires on a
health breach *even when all functional tests pass*. Stdlib only (no new deps); reads `/proc`
and the nginx access log directly (it runs on the host).

Monitored, with alert thresholds:

| Metric | Source | Alert when |
|--------|--------|-----------|
| CPU utilisation | two `/proc/stat` samples | ≥ 90% |
| Load average / core | `os.getloadavg()` | ≥ 4.0 per core |
| Memory used | `/proc/meminfo` | ≥ 90% |
| Disk used (`/`) | `os.statvfs` | ≥ 90% |
| **Request rate** (DDoS/runaway) | nginx access log, 5-min window | ≥ 1200 req/min sustained |
| **Request spike** | nginx access log | last-minute ≥ 5× window average (and ≥ 300 rpm) |
| **Error rate** | nginx access log, 5-min window | ≥ 5% `5xx` (min 30 reqs) |

Verified live: metrics collected correctly (cpu 1%, mem 43.6%, disk 38.4%, req-rate parsed from
the log) and a simulated breach builds + would send the Telegram alert. Request/error rates are
host-wide (the nginx log has no per-vhost field) — which is exactly the right scope for
"is the box about to fall over".

---

## TASK 4 — GDPR / minors in privacy policy

Added **Section 9 "Children & Minors"** to `frontend/privacy.html` (served at `/privacy`),
renumbered Security→10 and Contact→11, bumped the date to June 2026. It covers:

- Service is **18+**; account creation requires confirming age ≥ 18.
- **Under 13:** not directed to children; we do not knowingly collect their data (GDPR + COPPA);
  prompt deletion if discovered.
- **Under 18:** require **verifiable parental/guardian consent** where local law permits any use.
- **No targeted collection, behavioural advertising, or profiling of minors.**
- Parent/guardian contact route to review/correct/delete a minor's data.

---

## Fixes applied (all live + verified)

| Severity | Finding | Fix | File(s) |
|----------|---------|-----|---------|
| **Critical** | Content/AI endpoints (generate, generate_all, pipeline, video, voice, assemble, captions, upload_image, dm/reply, history, videos, voices, finals) had **no auth** → anonymous callers could trigger paid OpenAI/Runway/ElevenLabs work (cost-abuse / DoS). | Router-level `Depends(get_current_user)` on the entire content router → every route now `401` without a valid token. All frontend callers already send the bearer token via `apiFetch()`. | `routes/content.py` |
| **High** | **No logout / token revocation.** Stateless 30-day JWT stayed valid after logout. | New `POST /api/auth/logout` revokes the token (SHA-256 denylist with expiry purge); `get_current_user` rejects revoked tokens; frontend `logout()` now calls it. | `routes/auth.py`, `services/security.py`, `frontend/index.html` |
| **High** | **JWT secret failed open** to a public hardcoded default if `JWT_SECRET` was unset. | Fail **closed**: refuse to start if `JWT_SECRET` is missing or equals the old default. | `routes/auth.py` |
| **Medium** | `POST /api/billing/referral_signup` was an **unauthenticated HTTP route** that could forge referral relationships / invite bonuses for arbitrary `user_id`s. | Removed the route decorator — it is now an internal function only (its sole caller is `auth.register()`). | `routes/billing.py` |
| **Medium** | Missing **HSTS / CSP / Permissions-Policy** headers. | Added to the global security-headers middleware. | `services/security.py` |

---

## Larger work / recommendations (not breaches — logged for follow-up)

1. **Uniform `401` on unauthenticated requests (defense-in-depth).** Many endpoints
   (`/api/settings`, `/api/products/*`, `/api/scheduler/*`, `/api/email/*`, `/api/automation/*`,
   most platform-connect routes, etc.) currently return `200` with **empty/default data** when no
   token is present, instead of `401`, because they rely on the ContextVar store being empty.
   **This does not leak data** (verified — an empty context yields empty results and writes
   no-op). Recommended fix: a single global auth-gate middleware that enforces a valid user JWT
   for all `/api/*` paths except an explicit public allowlist (`/api/auth/*`, OAuth callbacks,
   `/api/billing/webhook`, `/api/webhooks/*`, `/api/telegram/webhook`, `/api/public/*`,
   `/api/admin/*` (own role auth), header-key routes). This must be done carefully and tested
   against the OAuth *connect* redirect flows (Instagram/TikTok/YouTube/X callbacks carry no user
   JWT), which is why it was not applied blind during this audit.

2. **Rate-limiter storage.** Move slowapi from per-worker in-memory to Redis so the global
   `60/min` limit is shared across workers/hosts and survives restarts.

3. **Rotate exposed secrets.** Tracked separately (see auto-memory "Secrets to rotate"): keys
   previously committed to git history still need rotating regardless of this audit.

4. **Tighten CSP `script-src`.** The current CSP intentionally does not restrict scripts (the
   frontend uses inline handlers). After refactoring inline JS, add a `script-src` with nonces.

5. **Repo hygiene.** Working tree contains junk dirs with trailing carriage-returns in their
   names (`routes\r`, `services\r`, `user_data\r`) — Windows line-ending artifacts, not used by
   the app. Safe to delete in a cleanup pass.

---

## Test artifacts

- `security_audit_test.py` — the live attack harness (creates two disposable `qa-` users,
  seeds distinct data, runs the full a–f matrix, cleans up).
- Re-run: `/opt/ugoingviral/bin/python security_audit_test.py` (note: it triggers the
  brute-force IP block by design; clear `security_log.json` `blocked`/`failures` to re-run
  back-to-back).
