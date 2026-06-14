"""
Live security-audit harness for UgoingViral. Runs real attacks against the
running server (localhost:8000) and prints PASS/FAIL per test.

Creates two disposable qa- users (A and B), force-verifies them directly in
users.db (same bypass the QA agent uses), logs in for real tokens, gives each
distinct data, then runs the attack matrix.

Run:  /opt/ugoingviral/bin/python security_audit_test.py   (or system python3)
"""
import json
import os
import sqlite3
import sys
import time

import httpx

BASE = "http://localhost:8000"
HERE = os.path.dirname(os.path.abspath(__file__))
USERS_DB = os.path.join(HERE, "users.db")
USER_DATA_DIR = os.path.join(HERE, "user_data")
PW = "QaTest2026!"

client = httpx.Client(base_url=BASE, timeout=30)
results = []


def rec(name, passed, detail=""):
    results.append((name, passed, detail))
    tag = "PASS" if passed else "FAIL"
    print(f"  [{tag}] {name}" + (f" — {detail}" if detail else ""))


def db_verify(email):
    """Force email_verified=True for a qa- user directly in users.db."""
    conn = sqlite3.connect(USERS_DB, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        row = conn.execute("SELECT id,data FROM users WHERE lower(email)=? LIMIT 1",
                           (email.lower(),)).fetchone()
        if not row:
            return None
        u = json.loads(row["data"])
        u["email_verified"] = True
        u["verification_code"] = ""
        conn.execute("UPDATE users SET data=? WHERE id=?",
                     (json.dumps(u, ensure_ascii=False), row["id"]))
        return u.get("id") or row["id"]
    finally:
        conn.close()


def make_user(label):
    ts = int(time.time() * 1000)
    email = f"qa-{label}-{ts}@ugoingviral.com"
    r = client.post("/api/auth/register", json={"email": email, "password": PW, "name": f"QA {label}"})
    if r.status_code not in (200, 201):
        print(f"register failed for {email}: {r.status_code} {r.text[:200]}")
        return None
    uid = db_verify(email)
    lr = client.post("/api/auth/login", json={"email": email, "password": PW})
    if lr.status_code != 200:
        print(f"login failed for {email}: {lr.status_code} {lr.text[:200]}")
        return None
    tok = lr.json().get("token")
    return {"email": email, "uid": uid, "token": tok, "h": {"Authorization": f"Bearer {tok}"}}


def cleanup(u):
    if not u:
        return
    try:
        conn = sqlite3.connect(USERS_DB, timeout=30.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("DELETE FROM users WHERE lower(email)=?", (u["email"].lower(),))
        conn.close()
    except Exception:
        pass
    for p in (os.path.join(USER_DATA_DIR, f"{u['uid']}.json"),):
        try:
            os.remove(p)
        except Exception:
            pass


def main():
    print("Creating test users (register has a 5/min limit; spacing requests)...")
    # Create all three up front, BEFORE the SQLi brute-force, so the failed-login
    # IP block can't prevent later logins.
    A = make_user("audA")
    time.sleep(13)
    B = make_user("audB")
    time.sleep(13)
    C = make_user("audC")
    if not A or not B or not C:
        print("Could not create test users — aborting.")
        sys.exit(1)
    print(f"User A uid={A['uid']}  User B uid={B['uid']}  User C uid={C['uid']}")

    # ── Seed distinct data into each user's store via authenticated calls ──
    # Settings carry a secret marker; schedule a post; generate content history.
    client.post("/api/settings", headers=A["h"], json={"settings": {"instagram_user": "SECRET_A_ig", "shopify_store": "store-A"}})
    client.post("/api/settings", headers=B["h"], json={"settings": {"instagram_user": "SECRET_B_ig", "shopify_store": "store-B"}})
    ra = client.post("/api/posts/schedule", headers=A["h"], json={"platform": "instagram", "content": "POST_BY_A", "scheduled_time": "2026-07-01T10:00:00"})
    rb = client.post("/api/posts/schedule", headers=B["h"], json={"platform": "instagram", "content": "POST_BY_B", "scheduled_time": "2026-07-01T10:00:00"})
    A_pid = ra.json().get("id") if ra.status_code == 200 else None
    B_pid = rb.json().get("id") if rb.status_code == 200 else None
    print(f"A post id={A_pid}  B post id={B_pid}")

    # ===================== (a) + (f) IDOR / user isolation =====================
    print("\n(a)+(f) IDOR / user isolation")

    # A reads own scheduled posts — should see POST_BY_A and NOT POST_BY_B
    r = client.get("/api/posts/scheduled", headers=A["h"])
    body = r.text
    rec("A's /posts/scheduled excludes B's data",
        ("POST_BY_A" in body) and ("POST_BY_B" not in body),
        f"status={r.status_code}")

    # A reads settings — should see SECRET_A and never SECRET_B
    r = client.get("/api/settings", headers=A["h"])
    rec("A's /api/settings excludes B's secret",
        ("SECRET_B_ig" not in r.text),
        f"A sees own={'SECRET_A_ig' in r.text}")

    # A tries to delete B's post by id (IDOR via path param)
    if B_pid:
        client.delete(f"/api/posts/{B_pid}", headers=A["h"])
        # Verify B's post still exists from B's view
        r = client.get("/api/posts/scheduled", headers=B["h"])
        rec("A cannot delete B's post via /posts/{id}", "POST_BY_B" in r.text,
            "B's post survived A's delete attempt")

    # A tries to reschedule B's post by id
    if B_pid:
        r = client.patch(f"/api/posts/{B_pid}/reschedule", headers=A["h"], json={"scheduled_time": "2030-01-01T00:00:00"})
        rec("A cannot reschedule B's post (404 expected)", r.status_code == 404,
            f"status={r.status_code}")

    # /api/auth/me reflects the token's own user only
    r = client.get("/api/auth/me", headers=A["h"])
    rec("/api/auth/me returns own identity", A["email"] in r.text and B["email"] not in r.text,
        f"status={r.status_code}")

    # ===================== (b) SQL injection =====================
    print("\n(b) SQL injection")
    payloads = ["admin'--", "admin' OR '1'='1", "' OR 1=1--", "'; DROP TABLE users;--",
                "\" OR \"\"=\"", "admin'/*", "' UNION SELECT * FROM users--"]
    sqli_ok = True
    details = []
    for p in payloads:
        r = client.post("/api/auth/login", json={"email": p, "password": p})
        # Must NOT return a token (200 with token). 400 (rejected) or 401 (invalid) are fine.
        leaked = r.status_code == 200 and "token" in r.text
        details.append(f"{p!r}->{r.status_code}")
        if leaked:
            sqli_ok = False
    rec("SQLi login payloads never authenticate", sqli_ok, "; ".join(details))
    # Verify users table intact
    try:
        conn = sqlite3.connect(USERS_DB)
        n = conn.execute("SELECT count(*) FROM users").fetchone()[0]
        conn.close()
        rec("users table intact after DROP TABLE payload", n > 0, f"{n} rows")
    except Exception as e:
        rec("users table intact after DROP TABLE payload", False, str(e))

    # ===================== (c) Session after logout =====================
    print("\n(c) Session after logout")
    # Use pre-created user C, capture token, hit logout (if any), reuse token.
    if C:
        before = client.get("/api/auth/me", headers=C["h"])
        # Try common logout endpoints
        lo_codes = {}
        for ep in ["/api/auth/logout", "/api/logout"]:
            lr = client.post(ep, headers=C["h"])
            lo_codes[ep] = lr.status_code
        after = client.get("/api/auth/me", headers=C["h"])
        # Test PASSES only if logout exists AND old token is rejected afterwards
        logout_exists = any(c not in (404, 405) for c in lo_codes.values())
        token_rejected = after.status_code == 401
        rec("Token rejected after logout", logout_exists and token_rejected,
            f"logout endpoints={lo_codes}; me before={before.status_code} after={after.status_code}")
        cleanup(C)

    # ===================== (d) Unauthenticated / invalid token =====================
    print("\n(d) Unauthenticated & invalid-token access")
    protected = [
        ("GET", "/api/auth/me"), ("GET", "/api/posts/scheduled"), ("GET", "/api/settings"),
        ("GET", "/api/billing/status"), ("GET", "/api/stats/overview"),
        ("GET", "/api/library/content"), ("GET", "/api/uploads/my"),
        ("GET", "/api/workspaces"), ("GET", "/api/subaccounts"),
        ("GET", "/api/notifications"), ("GET", "/api/analytics/dashboard"),
    ]
    bad = {"Authorization": "Bearer not.a.real.token"}
    forged = {"Authorization": "Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyX2lkIjoiZmFrZSJ9.x"}
    no_tok_results = []
    leaks = []
    for m, path in protected:
        r0 = client.request(m, path)               # no token
        r1 = client.request(m, path, headers=bad)  # invalid token
        no_tok_results.append(f"{path} no-tok={r0.status_code} bad-tok={r1.status_code}")
        # A leak = 200 that contains another user's secret / real data.
        for r in (r0, r1):
            if r.status_code == 200 and ("SECRET_A_ig" in r.text or "SECRET_B_ig" in r.text or "POST_BY_" in r.text):
                leaks.append(f"{path}:{r.status_code}")
    rec("No-token/invalid-token requests never leak another user's data", not leaks,
        ("LEAKS: " + ", ".join(leaks)) if leaks else "no data leaked")
    # Report which protected endpoints return 401 vs 200-empty (defense-in-depth gap)
    not_401 = []
    for m, path in protected:
        r0 = client.request(m, path)
        if r0.status_code != 401:
            not_401.append(f"{path}={r0.status_code}")
    rec("All protected endpoints return 401 without a token", not not_401,
        ("non-401: " + ", ".join(not_401)) if not_401 else "all 401")

    # Expensive AI endpoints reachable unauthenticated? (cost-abuse)
    r = client.post("/api/content/generate", json={"content_type": "caption", "platform": "instagram", "product_title": "x"})
    rec("AI endpoint /api/content/generate requires auth", r.status_code in (401, 403),
        f"status={r.status_code}")

    # ===================== (e) Admin access by a normal user =====================
    print("\n(e) Admin endpoint access by normal user")
    admin_eps = [
        ("GET", "/api/admin/users"), ("GET", "/api/admin/affiliates"),
        ("GET", "/api/admin/employees"), ("POST", "/api/admin/templates"),
        ("GET", "/api/health/detailed"),
    ]
    admin_ok = True
    adet = []
    for m, path in admin_eps:
        r = client.request(m, path, headers=A["h"], json={} if m == "POST" else None)
        adet.append(f"{path}={r.status_code}")
        if r.status_code not in (401, 403, 422):  # 422 = missing body but still not data
            # 200 would mean a normal user reached admin data
            if r.status_code == 200:
                admin_ok = False
    rec("Normal user cannot reach admin endpoints", admin_ok, "; ".join(adet))

    # Forged role=owner token signed with the USER secret must not work on admin
    # (admin uses a different secret). Try minting a user-secret token w/ role.
    try:
        from jose import jwt
        sys.path.insert(0, HERE)
        from routes.auth import SECRET_KEY, ALGORITHM
        forged_owner = jwt.encode({"user_id": A["uid"], "role": "owner", "sub": A["email"]}, SECRET_KEY, algorithm=ALGORITHM)
        r = client.get("/api/admin/users", headers={"Authorization": f"Bearer {forged_owner}"})
        rec("User-secret token with role=owner rejected by admin", r.status_code in (401, 403),
            f"status={r.status_code}")
    except Exception as e:
        rec("User-secret forged-owner test", False, f"err: {e}")

    # ── Summary ──
    npass = sum(1 for _, p, _ in results if p)
    print(f"\n==== {npass}/{len(results)} checks passed ====")

    cleanup(A)
    cleanup(B)

    print("\nJSON_RESULTS_START")
    print(json.dumps([{"name": n, "pass": p, "detail": d} for n, p, d in results], indent=2))
    print("JSON_RESULTS_END")


if __name__ == "__main__":
    main()
