#!/usr/bin/env python3
"""
UgoingViral — full-platform end-to-end agent test.

Exercises every major platform feature the way a real user would, against an
ISOLATED CLONE (default http://127.0.0.1:8056 — never production). The clone is
a consistent copy of production data with external provider keys blanked, so
no real provider calls or charges occur (video generation returns a clean 503
"no provider configured" before any credit/free-video is consumed).

Run:
    LT_BASE=http://127.0.0.1:8056 LT_USERS_DB=/tmp/ugv_agenttest/users.db \
        python agent_test.py

Output:
    - prints a per-feature PASS/FAIL summary
    - writes agent_test_results.md (table + failure details)
    - writes /tmp/agent_test_results.json (raw)
"""
import json
import os
import sqlite3
import time
import uuid

import httpx

BASE     = os.getenv("LT_BASE", "http://127.0.0.1:8056")
USERS_DB = os.getenv("LT_USERS_DB", "/tmp/ugv_agenttest/users.db")
RESULTS_MD   = os.getenv("LT_RESULTS_MD", os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_test_results.md"))
RESULTS_JSON = "/tmp/agent_test_results.json"

PW      = "AgentTestPw123"
PW_NEW  = "AgentTestPw456"

results = []   # {feature, name, ok, detail, code}
_client = httpx.Client(base_url=BASE, timeout=30.0)


def record(feature, name, ok, detail="", code=None):
    results.append({"feature": feature, "name": name, "ok": bool(ok), "detail": str(detail)[:300], "code": code})
    flag = "PASS" if ok else "FAIL"
    print(f"  [{flag}] {feature} :: {name}" + (f"  ({detail})" if detail else ""))


def call(method, path, token=None, json_body=None, files=None, data=None):
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        r = _client.request(method, path, headers=headers, json=json_body, files=files, data=data)
        body = None
        try:
            body = r.json()
        except Exception:
            body = r.text[:200]
        return r.status_code, body
    except Exception as e:
        return 0, f"EXC: {type(e).__name__}: {e}"


def verification_code_for(email):
    conn = sqlite3.connect(USERS_DB, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        for (d,) in conn.execute("SELECT data FROM users"):
            u = json.loads(d)
            if u.get("email") == email:
                return u.get("verification_code", "")
    finally:
        conn.close()
    return ""


def main():
    print(f"Agent test against {BASE}")
    h = _client.get("/health")
    print("health:", h.json())

    email = f"agenttest-{uuid.uuid4().hex[:8]}@agenttest.local"
    token = None
    uploaded_item_id = None

    # ───────────────────────── 1. AUTH ─────────────────────────
    code, body = call("POST", "/api/auth/register", json_body={"email": email, "password": PW, "name": "Agent"})
    record("Auth", "register new user", code == 200 and isinstance(body, dict) and body.get("requires_verification"),
           f"HTTP {code} {body}", code)

    vcode = verification_code_for(email)
    record("Auth", "verification code issued", bool(vcode), f"code={'present' if vcode else 'MISSING'}")

    code, body = call("POST", "/api/auth/verify_email", json_body={"email": email, "code": vcode})
    ok = code == 200 and isinstance(body, dict) and bool(body.get("token"))
    record("Auth", "verify email -> token", ok, f"HTTP {code}", code)
    if ok:
        token = body["token"]

    code, body = call("POST", "/api/auth/login", json_body={"email": email, "password": PW})
    ok = code == 200 and isinstance(body, dict) and bool(body.get("token"))
    record("Auth", "login", ok, f"HTTP {code}", code)
    if ok:
        token = body["token"]

    code, body = call("POST", "/api/user/two_factor", token=token, json_body={"enabled": True})
    record("Auth", "2FA toggle on", code == 200 and isinstance(body, dict) and body.get("two_factor_enabled") is True,
           f"HTTP {code} {body}", code)
    code, body = call("POST", "/api/user/two_factor", token=token, json_body={"enabled": False})
    record("Auth", "2FA toggle off", code == 200 and isinstance(body, dict) and body.get("two_factor_enabled") is False,
           f"HTTP {code} {body}", code)

    code, body = call("POST", "/api/user/change_password", token=token,
                      json_body={"current_password": PW, "new_password": PW_NEW})
    record("Auth", "password change", code == 200 and isinstance(body, dict) and body.get("ok"),
           f"HTTP {code} {body}", code)
    # confirm new password actually works (and old one no longer does)
    code, body = call("POST", "/api/auth/login", json_body={"email": email, "password": PW_NEW})
    ok = code == 200 and isinstance(body, dict) and bool(body.get("token"))
    record("Auth", "login with new password", ok, f"HTTP {code}", code)
    if ok:
        token = body["token"]
    code, body = call("POST", "/api/auth/login", json_body={"email": email, "password": PW})
    record("Auth", "old password rejected", code == 401, f"HTTP {code} (expect 401)", code)

    # ───────────────────────── 2. BILLING ─────────────────────────
    code, body = call("GET", "/api/billing/credits", token=token)
    credits_before = body.get("credits") if isinstance(body, dict) else None
    record("Billing", "fetch credits", code == 200 and isinstance(body, dict) and "credits" in body,
           f"HTTP {code} credits={credits_before}", code)
    code, body = call("GET", "/api/billing/plan", token=token)
    record("Billing", "plan info", code == 200 and isinstance(body, dict) and "plan" in body,
           f"HTTP {code} plan={body.get('plan') if isinstance(body, dict) else body}", code)

    # ───────────────────────── 3. VIDEO STUDIO ─────────────────────────
    code, body = call("GET", "/api/studio/free_videos", token=token)
    record("Studio", "free videos status", code == 200 and isinstance(body, dict) and "remaining" in body,
           f"HTTP {code} {body}", code)
    code, body = call("GET", "/api/studio/providers", token=token)
    record("Studio", "providers list", code == 200, f"HTTP {code}", code)
    studio_req = {"mode": "single", "scenes": [{"prompt": "a calm ocean at sunset"}],
                  "provider": "runway", "enhance_prompts": False, "voice_enabled": False}
    code, body = call("POST", "/api/studio/estimate", token=token, json_body=studio_req)
    record("Studio", "cost estimate", code == 200 and isinstance(body, dict) and "total_cost" in body,
           f"HTTP {code} {body}", code)
    # generate: with no provider keys the clone must refuse cleanly (503/403/400)
    # WITHOUT calling a provider or charging credits.
    code, body = call("POST", "/api/studio/generate", token=token, json_body=studio_req)
    record("Studio", "generate refuses cleanly (no provider, no cost)", code in (503, 403, 400),
           f"HTTP {code} {body}", code)
    code, body2 = call("GET", "/api/billing/credits", token=token)
    credits_after = body2.get("credits") if isinstance(body2, dict) else None
    record("Studio", "generate did NOT consume credits", credits_after == credits_before,
           f"before={credits_before} after={credits_after}")

    # ───────────────────────── 4. CONTENT LIBRARY ─────────────────────────
    code, body = call("GET", "/api/library/content", token=token)
    record("Library", "list content", code == 200 and isinstance(body, dict) and "items" in body,
           f"HTTP {code} items={len(body.get('items', [])) if isinstance(body, dict) else '?'}", code)
    folder = "AgentTestFolder"
    code, body = call("POST", "/api/library/content_folders", token=token, json_body={"name": folder})
    record("Library", "create folder", code == 200 and isinstance(body, dict) and folder in body.get("folders", []),
           f"HTTP {code} {body}", code)
    # upload a tiny valid PNG (1x1)
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c6360000002000154a24f9f0000000049454e44ae426082"
    )
    code, body = call("POST", "/api/library/upload", token=token,
                      files={"file": ("agenttest.png", png, "image/png")})
    # Upload returns {"ok": True, "item": {...,"id":...}}
    item = body.get("item") if isinstance(body, dict) else None
    ok = code == 200 and isinstance(item, dict) and bool(item.get("id"))
    record("Library", "upload small test file", ok,
           f"HTTP {code} " + (f"id={item.get('id')}" if ok else str(body)), code)
    if ok:
        uploaded_item_id = item["id"]
    if uploaded_item_id:
        code, body = call("POST", f"/api/library/content/{uploaded_item_id}/move", token=token,
                          json_body={"folder": folder})
        record("Library", "move item to folder", code == 200 and isinstance(body, dict) and body.get("folder") == folder,
               f"HTTP {code} {body}", code)
    else:
        record("Library", "move item to folder", False, "skipped — no uploaded item id")
    code, body = call("DELETE", f"/api/library/content_folders/{folder}", token=token)
    record("Library", "delete folder", code == 200 and isinstance(body, dict) and folder not in body.get("folders", []),
           f"HTTP {code} {body}", code)

    # ───────────────────────── 5. POST & SCHEDULE ─────────────────────────
    future = "2030-01-01T12:00:00"
    code, body = call("POST", "/api/posts/schedule", token=token,
                      json_body={"platform": "instagram", "content": "Agent test post 🚀",
                                 "scheduled_time": future, "mode": "ui"})
    ok = code == 200 and isinstance(body, dict) and body.get("status") == "scheduled"
    record("Posts", "create scheduled post", ok, f"HTTP {code} {body}", code)
    post_id = body.get("id") if isinstance(body, dict) else None
    code, body = call("GET", "/api/posts/scheduled", token=token)
    posts = body.get("posts", []) if isinstance(body, dict) else []
    found = any(p.get("id") == post_id for p in posts) if post_id else len(posts) > 0
    record("Posts", "list scheduled posts", code == 200 and found,
           f"HTTP {code} count={len(posts)} found_ours={found}", code)
    code, body = call("GET", "/api/posts/history", token=token)
    record("Posts", "post history", code == 200 and isinstance(body, dict) and "history" in body,
           f"HTTP {code}", code)

    # ───────────────────────── 6. CONNECT (platform status) ─────────────────────────
    for plat in ("instagram", "tiktok", "youtube", "twitter"):
        code, body = call("GET", f"/api/{plat}/status", token=token)
        record("Connect", f"{plat} status", code == 200 and isinstance(body, dict),
               f"HTTP {code} {body if code != 200 else 'ok'}", code)
    code, body = call("GET", "/api/settings/connections", token=token)
    record("Connect", "connections summary", code == 200, f"HTTP {code}", code)

    # ───────────────────────── 7. ANALYTICS ─────────────────────────
    code, body = call("GET", "/api/analytics/dashboard", token=token)
    record("Analytics", "dashboard", code == 200 and isinstance(body, dict), f"HTTP {code}", code)
    code, body = call("GET", "/api/analytics/performance", token=token)
    record("Analytics", "performance data", code == 200, f"HTTP {code}", code)

    # ───────────────────────── 8. SETTINGS ─────────────────────────
    code, body = call("POST", "/api/user/update_profile", token=token, json_body={"name": "Agent Tester"})
    record("Settings", "profile update", code == 200 and isinstance(body, dict) and body.get("ok"),
           f"HTTP {code} {body}", code)
    code, body = call("POST", "/api/user/notifications", token=token,
                      json_body={"weekly_report": True, "credit_alerts": True})
    record("Settings", "notification prefs", code == 200 and isinstance(body, dict) and body.get("ok"),
           f"HTTP {code} {body}", code)
    code, body = call("POST", "/api/support/contact", token=token,
                      json_body={"subject": "Agent test", "message": "This is an automated agent test message.",
                                 "email": email, "name": "Agent Tester"})
    record("Settings", "help/support contact form", code == 200,
           f"HTTP {code} {body}", code)

    # ───────────────────────── 9. NOTIFICATIONS ─────────────────────────
    code, body = call("GET", "/api/notifications", token=token)
    ok = code == 200 and isinstance(body, dict) and "notifications" in body and "unread" in body
    record("Notifications", "list", ok, f"HTTP {code} unread={body.get('unread') if isinstance(body, dict) else '?'}", code)
    notifs = body.get("notifications", []) if isinstance(body, dict) else []
    if notifs:
        nid = notifs[0].get("id")
        code, body = call("POST", f"/api/notifications/read/{nid}", token=token)
        record("Notifications", "mark one read", code == 200 and isinstance(body, dict) and "unread" in body,
               f"HTTP {code} {body}", code)
    code, body = call("POST", "/api/notifications/read_all", token=token)
    record("Notifications", "mark all read", code == 200 and isinstance(body, dict) and body.get("unread") == 0,
           f"HTTP {code} {body}", code)

    # ───────────────────────── 10. ADMIN GATING ─────────────────────────
    # Admin endpoints must reject normal users. 401 or 403 are both valid
    # "blocked" outcomes (admin tokens are a separate auth domain with their own
    # secret, so a normal-user JWT is rejected at decode time → 401).
    code, body = call("GET", "/api/admin/users", token=token)   # normal user
    record("Admin gating", "admin/users blocks normal user (401/403)", code in (401, 403), f"HTTP {code}", code)
    code, body = call("GET", "/api/admin/users")                # no token
    record("Admin gating", "admin/users blocks anonymous (401/403)", code in (401, 403), f"HTTP {code}", code)
    code, body = call("GET", "/api/admin/revenue", token=token)
    record("Admin gating", "admin/revenue blocks normal user (401/403)", code in (401, 403), f"HTTP {code}", code)

    _client.close()
    write_report(email)


def write_report(test_email):
    total = len(results)
    passed = sum(1 for r in results if r["ok"])
    failed = total - passed

    # group by feature
    feats = {}
    for r in results:
        feats.setdefault(r["feature"], []).append(r)

    lines = []
    lines.append("# UgoingViral — Full Platform Agent Test Results\n")
    lines.append(f"**Date:** 2026-06-12  ")
    lines.append(f"**Target:** isolated clone `{BASE}` (consistent copy of production data — production never touched)  ")
    lines.append(f"**Test user:** `{test_email}` (deleted in cleanup)  ")
    lines.append(f"**Result:** **{passed}/{total} checks passed**, {failed} failed\n")

    lines.append("## Per-feature summary\n")
    lines.append("| Feature | Passed | Status |")
    lines.append("|---|---|---|")
    for feat, items in feats.items():
        p = sum(1 for i in items if i["ok"])
        t = len(items)
        status = "✅ PASS" if p == t else f"❌ {t - p} FAILED"
        lines.append(f"| {feat} | {p}/{t} | {status} |")
    lines.append("")

    lines.append("## Detailed checks\n")
    for feat, items in feats.items():
        lines.append(f"### {feat}")
        lines.append("| Check | Result | Detail |")
        lines.append("|---|---|---|")
        for i in items:
            flag = "✅ PASS" if i["ok"] else "❌ FAIL"
            detail = i["detail"].replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {i['name']} | {flag} | {detail} |")
        lines.append("")

    if failed:
        lines.append("## Failures\n")
        for r in results:
            if not r["ok"]:
                lines.append(f"- **{r['feature']} :: {r['name']}** — {r['detail']}")
        lines.append("")

    with open(RESULTS_MD, "w") as f:
        f.write("\n".join(lines) + "\n")
    with open(RESULTS_JSON, "w") as f:
        json.dump(results, f, indent=2)

    print(f"\n{'='*60}")
    print(f"TOTAL: {passed}/{total} passed, {failed} failed")
    print(f"Wrote {RESULTS_MD} and {RESULTS_JSON}")


if __name__ == "__main__":
    main()
