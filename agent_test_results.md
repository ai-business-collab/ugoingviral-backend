# UgoingViral — Full Platform Agent Test Results

**Date:** 2026-06-12  
**Target:** isolated clone `http://127.0.0.1:8056` (consistent copy of production data — production never touched)  
**Test user:** `agenttest-75d652ae@agenttest.local` (deleted in cleanup)  
**Result:** **40/40 checks passed**, 0 failed

## Per-feature summary

| Feature | Passed | Status |
|---|---|---|
| Auth | 9/9 | ✅ PASS |
| Billing | 2/2 | ✅ PASS |
| Studio | 5/5 | ✅ PASS |
| Library | 5/5 | ✅ PASS |
| Posts | 3/3 | ✅ PASS |
| Connect | 5/5 | ✅ PASS |
| Analytics | 2/2 | ✅ PASS |
| Settings | 3/3 | ✅ PASS |
| Notifications | 3/3 | ✅ PASS |
| Admin gating | 3/3 | ✅ PASS |

## Detailed checks

### Auth
| Check | Result | Detail |
|---|---|---|
| register new user | ✅ PASS | HTTP 200 {'requires_verification': True, 'email': 'agenttest-75d652ae@agenttest.local'} |
| verification code issued | ✅ PASS | code=present |
| verify email -> token | ✅ PASS | HTTP 200 |
| login | ✅ PASS | HTTP 200 |
| 2FA toggle on | ✅ PASS | HTTP 200 {'ok': True, 'two_factor_enabled': True} |
| 2FA toggle off | ✅ PASS | HTTP 200 {'ok': True, 'two_factor_enabled': False} |
| password change | ✅ PASS | HTTP 200 {'ok': True} |
| login with new password | ✅ PASS | HTTP 200 |
| old password rejected | ✅ PASS | HTTP 401 (expect 401) |

### Billing
| Check | Result | Detail |
|---|---|---|
| fetch credits | ✅ PASS | HTTP 200 credits=55 |
| plan info | ✅ PASS | HTTP 200 plan=free |

### Studio
| Check | Result | Detail |
|---|---|---|
| free videos status | ✅ PASS | HTTP 200 {'plan': 'free', 'limit': 3, 'used': 0, 'remaining': 3, 'eligible': True, 'duration': 10} |
| providers list | ✅ PASS | HTTP 200 |
| cost estimate | ✅ PASS | HTTP 200 {'total_cost': 40, 'credits_available': 55, 'can_afford': True, 'breakdown': {'scenes': 1, 'provider': 'runway', 'voice': 0, 'enhance': 0}} |
| generate refuses cleanly (no provider, no cost) | ✅ PASS | HTTP 503 {'detail': 'Ingen AI video provider er konfigureret. Tilføj RUNWAY_API_KEY, LUMA_API_KEY eller REPLICATE_API_KEY i .env'} |
| generate did NOT consume credits | ✅ PASS | before=55 after=55 |

### Library
| Check | Result | Detail |
|---|---|---|
| list content | ✅ PASS | HTTP 200 items=0 |
| create folder | ✅ PASS | HTTP 200 {'ok': True, 'folders': ['AgentTestFolder']} |
| upload small test file | ✅ PASS | HTTP 200 id=8764a8cdc751 |
| move item to folder | ✅ PASS | HTTP 200 {'ok': True, 'id': '8764a8cdc751', 'folder': 'AgentTestFolder'} |
| delete folder | ✅ PASS | HTTP 200 {'ok': True, 'folders': []} |

### Posts
| Check | Result | Detail |
|---|---|---|
| create scheduled post | ✅ PASS | HTTP 200 {'status': 'scheduled', 'id': '2026-06-12T02:44:22.195513'} |
| list scheduled posts | ✅ PASS | HTTP 200 count=1 found_ours=True |
| post history | ✅ PASS | HTTP 200 |

### Connect
| Check | Result | Detail |
|---|---|---|
| instagram status | ✅ PASS | HTTP 200 ok |
| tiktok status | ✅ PASS | HTTP 200 ok |
| youtube status | ✅ PASS | HTTP 200 ok |
| twitter status | ✅ PASS | HTTP 200 ok |
| connections summary | ✅ PASS | HTTP 200 |

### Analytics
| Check | Result | Detail |
|---|---|---|
| dashboard | ✅ PASS | HTTP 200 |
| performance data | ✅ PASS | HTTP 200 |

### Settings
| Check | Result | Detail |
|---|---|---|
| profile update | ✅ PASS | HTTP 200 {'ok': True} |
| notification prefs | ✅ PASS | HTTP 200 {'ok': True, 'notifications': {'failed_posts': True, 'credit_warnings': True, 'weekly_summary': False, 'product_updates': True}} |
| help/support contact form | ✅ PASS | HTTP 200 {'status': 'queued', 'message': "We received your message, we'll reply within 24 hours", 'to': 'support@ugoingviral.com', 'inbox_delivered': False} |

### Notifications
| Check | Result | Detail |
|---|---|---|
| list | ✅ PASS | HTTP 200 unread=1 |
| mark one read | ✅ PASS | HTTP 200 {'ok': True, 'unread': 0} |
| mark all read | ✅ PASS | HTTP 200 {'ok': True, 'unread': 0} |

### Admin gating
| Check | Result | Detail |
|---|---|---|
| admin/users blocks normal user (401/403) | ✅ PASS | HTTP 401 |
| admin/users blocks anonymous (401/403) | ✅ PASS | HTTP 401 |
| admin/revenue blocks normal user (401/403) | ✅ PASS | HTTP 401 |


## Bugs fixed during this run

All 40 checks pass. No **platform** bugs surfaced in the tested paths — every
feature responded correctly. The only defects found were in the **test script
itself** (first run: 36/40), and were fixed directly in `agent_test.py`:

1. **Library upload assertion** — the upload endpoint returns
   `{"ok": true, "item": {…, "id": …}}`; the test was reading `id` from the top
   level instead of `item.id`, so it wrongly logged FAIL (and the follow-on
   "move item" check was skipped). The upload itself worked (HTTP 200, file
   saved, library entry created). Fixed to read `item.id`.
2. **Admin-gating assertion too strict** — the test demanded HTTP 403 for a
   normal user, but admin endpoints are a separate auth domain (own JWT secret),
   so a normal-user token is rejected at decode time with **401**. That is
   correct, secure gating, and the task accepts "401/403 for normal users".
   Fixed to accept either 401 or 403.

After these test-script fixes: **40/40 PASS**.

## Notes / issues found (for follow-up, not test failures)

These are code-review observations made while building the test. They did NOT
cause failures (the endpoints behave correctly when called with a valid token,
as the test confirms), so they are listed here rather than fixed under this
task's narrow commit scope (`agent_test.py` + `agent_test_results.md` only).

1. **Posts endpoints have no explicit auth dependency** (`routes/posts.py`:
   `schedule_post`, `get_scheduled`, `get_post_history`, and the bulk-delete
   handlers). They operate on the per-user `store` resolved from the auth
   middleware's context var rather than `Depends(get_current_user)`. With a
   valid Bearer token they work correctly (verified). But an *unauthenticated*
   request is not rejected with 401 — it silently reads/writes an empty default
   store context. **Recommendation:** add `Depends(get_current_user)` to these
   handlers for consistent auth and proper 401s. *(Severity: medium — no data
   leak between users since the context is empty without a token, but the
   endpoints should fail closed.)*

2. **`store.get("scheduled_posts", {})` uses a dict default for a list field**
   (`routes/posts.py`, several spots, e.g. `…{}).insert(0, item)` and
   `len(…{})`). Harmless today because `DEFAULT_STORE` always seeds
   `scheduled_posts: []`, so the key is never actually missing — but if it were,
   `.insert()` on a `{}` would raise `AttributeError`. **Recommendation:** change
   the defaults to `[]`. *(Severity: low — latent, non-triggering.)*

## Methodology

- Ran against an **isolated clone** on `127.0.0.1:8056` — a consistent copy
  (SQLite backup API) of production `users.db` (1,953 users). **Production was
  never touched.**
- External provider keys (Runway/Luma/Replicate/OpenAI/Stripe/SendGrid) were
  blanked in the clone, so **no provider calls and no charges** occurred. Video
  generation returns a clean `503 "no provider configured"` *before* any credit
  or free-video is consumed — verified explicitly (credits 55 → 55 across the
  generate call).
- The slowapi rate limiter was disabled in the clone for deterministic
  functional testing (rate limiting is validated separately and is not one of
  the 10 feature areas).
- A fresh throwaway user was registered per run; the clone and all test data are
  deleted in cleanup.
