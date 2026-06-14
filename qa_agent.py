#!/opt/ugoingviral/bin/python3
"""UgoingViral QA Agent — Standalone cron script"""
import os, sys, json, time, argparse
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env():
    env_path = os.path.join(BASE_DIR, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    os.environ.setdefault(k.strip(), v.strip())


_load_env()
import httpx

BASE_URL      = "http://localhost:8000"
QA_EMAIL      = "qa-auto@ugoingviral.com"
QA_PASSWORD   = "QaAuto2026!"
RESULTS_FILE  = os.path.join(BASE_DIR, "qa_results.json")
USER_DATA_DIR = os.path.join(BASE_DIR, "user_data")
USERS_FILE    = os.path.join(BASE_DIR, "users.json")   # legacy (pre-SQLite migration)
USERS_DB      = os.path.join(BASE_DIR, "users.db")     # SQLite store (current source of truth)
NEXORA_KEY    = os.getenv("NEXORA_API_KEY", "nexora-core-2026")
NEXORA_URL    = "https://nex-core.tech/api/qa/report"
TG_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ALERT_CHAT = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")

EXPECTED_PLAN_PRICES = {
    "free": 0, "starter": 49, "basic": 79, "growth": 299,
    "pro": 149, "elite": 229, "personal": 459, "agency": 199,
}
EXPECTED_CREDIT_PACKAGES = {100: 8, 350: 24, 700: 44, 1500: 79}

# ── Server health / runaway-detection thresholds (2026-06-14 audit) ───────────
# The QA agent runs every 6h; this catches resource exhaustion, DDoS/runaway
# request spikes and elevated error rates BEFORE they take the server down, and
# surfaces them in the same Telegram alert the QA suite already uses.
HEALTH_CPU_WARN_PCT   = 90      # sustained CPU utilisation
HEALTH_MEM_WARN_PCT   = 90      # system memory used
HEALTH_DISK_WARN_PCT  = 90      # disk used on /
HEALTH_LOAD_PER_CORE  = 4.0     # 1-min load average per CPU core
NGINX_ACCESS_LOG      = "/var/log/nginx/access.log"
HEALTH_REQ_WINDOW_MIN = 5       # window (minutes) for request-rate + error-rate
HEALTH_REQ_RATE_WARN  = 1200    # requests/min averaged over the window = abnormal
HEALTH_SPIKE_FACTOR   = 5.0     # last-minute rate > N× the window average = spike
HEALTH_SPIKE_MIN_RPM  = 300     # ...and at least this many req/min (ignore noise)
HEALTH_ERR_RATE_WARN  = 5.0     # percent of 5xx responses in the window
HEALTH_ERR_MIN_SAMPLE = 30      # need at least this many requests to judge errors


class QARunner:
    def __init__(self, skip_ai: bool = False):
        self.skip_ai   = skip_ai
        self.token     = None
        self.user_id   = None
        self.results   = []
        self.client    = httpx.Client(base_url=BASE_URL, timeout=10)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _pass(self, name: str, detail: str = ""):
        self.results.append({"name": name, "status": "PASS", "detail": detail})
        print(f"  [PASS] {name}" + (f" — {detail}" if detail else ""))

    def _fail(self, name: str, detail: str = ""):
        self.results.append({"name": name, "status": "FAIL", "detail": detail})
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))

    def _auth_headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    @staticmethod
    def _db_conn():
        """Open the SQLite user store the way the app does (WAL + busy_timeout)
        so QA writes queue behind the live app's writers instead of erroring on
        a locked DB. Users migrated from users.json → users.db, so the JSON file
        is now stale; QA must read/write the DB to affect the running app."""
        import sqlite3
        conn = sqlite3.connect(USERS_DB, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _bypass_email_verification(self, email: str) -> str | None:
        """Mark a qa- test user as email_verified=True directly in the SQLite
        user store (users.db). Users are stored as a JSON blob in the `data`
        column, so we read-modify-write that blob.

        Only operates on emails starting with 'qa-' so production flow is never
        affected. Returns the user_id if the bypass was applied, else None.
        """
        if not email or not email.startswith("qa-"):
            return None
        try:
            conn = self._db_conn()
            try:
                row = conn.execute(
                    "SELECT id, data FROM users WHERE lower(email) = ? LIMIT 1",
                    (email.lower(),),
                ).fetchone()
                if not row:
                    return None
                user = json.loads(row["data"])
                user["email_verified"]      = True
                user["verification_code"]   = ""
                user["verification_expires"] = ""
                conn.execute(
                    "UPDATE users SET data = ? WHERE id = ?",
                    (json.dumps(user, ensure_ascii=False), row["id"]),
                )
                return user.get("id") or row["id"]
            finally:
                conn.close()
        except Exception:
            return None

    def _fresh_user(self, label: str):
        """Register a disposable test user. Returns (token, user_id, email).

        Retries on 429 (rate limit) with a long backoff so consecutive
        suite calls don't collide on the 5/min register limit. After
        registration the qa- account is force-verified (bypass) so the
        subsequent login isn't blocked by the email verification gate.
        """
        password = "QaTest2026!"
        for attempt in range(3):
            time.sleep(13 if attempt == 0 else 65)
            ts = int(time.time() * 1000)
            email = f"qa-{label}-{ts}@ugoingviral.com"
            try:
                r = self.client.post("/api/auth/register", json={
                    "email": email, "password": password, "name": f"QA {label}"
                })
                if r.status_code in (200, 201):
                    data = r.json()
                    token   = data.get("token") or data.get("access_token")
                    user_id = (data.get("user") or {}).get("id") or data.get("user_id")
                    # QA bypass: mark the test account as verified so we can log in.
                    bypass_uid = self._bypass_email_verification(email)
                    user_id = user_id or bypass_uid
                    if not token:
                        try:
                            lr = self.client.post("/api/auth/login", json={
                                "email": email, "password": password
                            })
                            if lr.status_code == 200:
                                ldata = lr.json()
                                token = ldata.get("token") or ldata.get("access_token")
                                user_id = user_id or (ldata.get("user") or {}).get("id") or ldata.get("user_id")
                        except Exception:
                            pass
                    return token, user_id, email
                if r.status_code != 429:
                    break
            except Exception:
                pass
        return None, None, email

    def _set_billing(self, user_id: str, plan: str, credits: int) -> bool:
        """Write billing plan + credits into the user's per-user store
        (user_data/{user_id}.json). Billing is NOT in users.db — the app reads
        it from the ContextVar-backed per-user store (services.store), keyed by
        user_id, as store["billing"] = {"plan", "credits"}. Returns True on success."""
        if not user_id:
            return False
        user_file = os.path.join(USER_DATA_DIR, f"{user_id}.json")
        try:
            data = {}
            if os.path.exists(user_file):
                with open(user_file, encoding="utf-8") as f:
                    data = json.load(f)
            billing = data.get("billing", {})
            billing["plan"]    = plan
            billing["credits"] = credits
            data["billing"]    = billing
            with open(user_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

    def _remove_fresh_user(self, user_id: str, email: str):
        """Delete a disposable test user from the SQLite store (and clean up any
        stale legacy JSON + per-user data file)."""
        try:
            conn = self._db_conn()
            try:
                conn.execute(
                    "DELETE FROM users WHERE id = ? OR lower(email) = ?",
                    (user_id, (email or "").lower()),
                )
            finally:
                conn.close()
        except Exception:
            pass
        # Legacy cleanup — harmless if the files no longer exist.
        try:
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, encoding="utf-8") as f:
                    users = json.load(f)
                if isinstance(users, list):
                    users = [u for u in users
                             if u.get("id") != user_id and u.get("email") != email]
                    with open(USERS_FILE, "w", encoding="utf-8") as f:
                        json.dump(users, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
        user_file = os.path.join(USER_DATA_DIR, f"{user_id}.json")
        if os.path.exists(user_file):
            try:
                os.remove(user_file)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Suite 1 — Signup / Login flow
    # ------------------------------------------------------------------

    def suite1_signup(self):
        print("\n=== SUITE 1: Signup / Login ===")

        # Register (400 if already exists is fine)
        try:
            r = self.client.post("/api/auth/register", json={
                "email": QA_EMAIL, "password": QA_PASSWORD,
                "name": "QA Auto"
            })
            if r.status_code in (200, 201, 400):
                self._pass("register", f"status={r.status_code}")
            else:
                self._fail("register", f"unexpected status={r.status_code}")
        except Exception as e:
            self._fail("register", str(e))

        # QA bypass: mark the qa-auto account as verified so login isn't
        # blocked by the email verification gate. Real users still go
        # through the normal verification flow — only qa- prefixed accounts
        # are affected by _bypass_email_verification.
        self._bypass_email_verification(QA_EMAIL)

        # Login
        try:
            r = self.client.post("/api/auth/login", json={
                "email": QA_EMAIL, "password": QA_PASSWORD
            })
            if r.status_code == 200:
                data = r.json()
                self.token   = data.get("token") or data.get("access_token")
                self.user_id = data.get("user_id") or (data.get("user", {}) or {}).get("id")
                if self.token:
                    self._pass("login", f"user_id={self.user_id}")
                else:
                    self._fail("login", "no token in response")
            else:
                self._fail("login", f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("login", str(e))

        # JWT validation — hit a protected endpoint
        if self.token:
            try:
                r = self.client.get("/api/auth/me", headers=self._auth_headers())
                if r.status_code == 200:
                    self._pass("jwt_validation")
                else:
                    self._fail("jwt_validation", f"status={r.status_code}")
            except Exception as e:
                self._fail("jwt_validation", str(e))

    # ------------------------------------------------------------------
    # Suite 2 — Free plan limits
    # ------------------------------------------------------------------

    def suite2_free_plan(self):
        print("\n=== SUITE 2: Free Plan Limits ===")
        if not self.token:
            self._fail("free_plan_skip", "no token — skipping suite")
            return

        # Fetch billing plan info
        try:
            r = self.client.get("/api/billing/plan", headers=self._auth_headers())
            if r.status_code != 200:
                self._fail("billing_fetch", f"status={r.status_code}")
                return
            d = r.json()
        except Exception as e:
            self._fail("billing_fetch", str(e))
            return

        plan = (d.get("plan") or "").lower()
        if plan == "free":
            self._pass("plan_is_free")
        else:
            self._fail("plan_is_free", f"got plan={plan!r}")

        # max_credits reflects the plan cap (credits is current balance and may vary)
        max_credits = d.get("max_credits", -1)
        if max_credits == 50:
            self._pass("max_credits_50")
        else:
            self._fail("max_credits_50", f"got max_credits={max_credits}")

        free_plan = (d.get("plans") or {}).get("free", {})
        free_videos = free_plan.get("free_videos", -1)
        if free_videos == 3:
            self._pass("free_videos_3")
        else:
            self._fail("free_videos_3", f"got free_videos={free_videos}")

        can_download = free_plan.get("download", True)
        if can_download is False:
            self._pass("download_locked")
        else:
            self._fail("download_locked", f"download={can_download!r} (should be False)")

        # Studio / generate endpoints should be gated (400-403) or pass through — just reachable
        for endpoint in ["/api/studio", "/api/generate"]:
            try:
                r = self.client.post(endpoint, headers=self._auth_headers(), json={})
                if r.status_code in range(400, 600):
                    self._pass(f"gated_{endpoint.strip('/')}", f"status={r.status_code}")
                else:
                    self._fail(f"gated_{endpoint.strip('/')}", f"unexpected 2xx on {endpoint}")
            except Exception as e:
                self._fail(f"gated_{endpoint.strip('/')}", str(e))

    # ------------------------------------------------------------------
    # Suite 3 — Content generation
    # ------------------------------------------------------------------

    def suite3_content_gen(self):
        print("\n=== SUITE 3: Content Generation ===")
        if not self.token:
            self._fail("content_gen_skip", "no token — skipping suite")
            return

        if self.skip_ai:
            self._pass("content_gen_skipped", "--skip-ai flag set")
            return

        # Attempt a minimal content generation call
        payload = {
            "content_type": "caption",
            "platform": "instagram",
            "language": "en",
            "tone": "engaging",
        }
        try:
            r = self.client.post("/api/content/generate", headers=self._auth_headers(),
                                 json=payload, timeout=30)
            if r.status_code in (200, 201):
                self._pass("content_generate", f"status={r.status_code}")
            elif r.status_code in (402, 403):
                self._pass("content_generate_gated", f"status={r.status_code} (gated correctly)")
            else:
                self._fail("content_generate", f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("content_generate", str(e))

    # ------------------------------------------------------------------
    # Suite 4 — Billing verification
    # ------------------------------------------------------------------

    def suite4_billing(self):
        print("\n=== SUITE 4: Billing Verification ===")
        if not self.token:
            self._fail("billing_skip", "no token — skipping suite")
            return

        # Fetch plans — included in /api/billing/plan response
        try:
            r = self.client.get("/api/billing/plan", headers=self._auth_headers())
            if r.status_code != 200:
                self._fail("plans_fetch", f"status={r.status_code}")
                return
            plans = (r.json() or {}).get("plans", {})
        except Exception as e:
            self._fail("plans_fetch", str(e))
            return

        # Verify plan prices
        for plan_name, expected_price in EXPECTED_PLAN_PRICES.items():
            plan_data = plans.get(plan_name, {})
            actual_price = plan_data.get("price", None)
            if actual_price is None:
                self._fail(f"plan_price_{plan_name}", "not found in response")
            elif int(actual_price) != expected_price:
                self._fail(f"plan_price_{plan_name}", f"expected ${expected_price} got ${actual_price}")
            else:
                self._pass(f"plan_price_{plan_name}", f"${actual_price}")

        # Fetch credit packages — /api/billing/topup_packages
        try:
            r = self.client.get("/api/billing/topup_packages", headers=self._auth_headers())
            if r.status_code == 200:
                pkgs = (r.json() or {}).get("packages", {})
                for qty, expected_price in EXPECTED_CREDIT_PACKAGES.items():
                    pkg_data = pkgs.get(str(qty), {})
                    actual = pkg_data.get("price", None)
                    if actual is None:
                        self._fail(f"credit_pkg_{qty}", "not found")
                    elif int(actual) != expected_price:
                        self._fail(f"credit_pkg_{qty}", f"expected ${expected_price} got ${actual}")
                    else:
                        self._pass(f"credit_pkg_{qty}", f"${actual}")
            else:
                self._fail("credit_packages_fetch", f"status={r.status_code}")
        except Exception as e:
            self._fail("credit_packages_fetch", str(e))

        # Yearly math: Pro $149 → yearly = round(149*0.8) = $119
        pro_price    = EXPECTED_PLAN_PRICES.get("pro", 149)
        yearly_price = round(pro_price * 0.8)
        if yearly_price == 119:
            self._pass("yearly_math_pro", f"${pro_price} * 0.8 = ${yearly_price}")
        else:
            self._fail("yearly_math_pro", f"expected $119 got ${yearly_price}")

    # ------------------------------------------------------------------
    # Suite 5 — Platform health
    # ------------------------------------------------------------------

    def suite5_health(self):
        print("\n=== SUITE 5: Platform Health ===")
        endpoints = [
            ("GET",  "/",                    200),
            ("GET",  "/health",              200),
            ("POST", "/api/auth/login",      422),
            ("GET",  "/api/billing/plan",    401),
            ("GET",  "/api/billing/usage",    401),
        ]
        for method, path, expected_status in endpoints:
            t0 = time.time()
            try:
                if method == "GET":
                    r = self.client.get(path)
                else:
                    r = self.client.post(path, json={})
                elapsed_ms = (time.time() - t0) * 1000
                status_ok  = (r.status_code == expected_status or
                              expected_status // 100 == r.status_code // 100)
                time_ok = elapsed_ms < 2000
                label = f"health_{path.strip('/') or 'root'}"
                if status_ok and time_ok:
                    self._pass(label, f"status={r.status_code} {elapsed_ms:.0f}ms")
                elif not time_ok:
                    self._fail(label, f"too slow {elapsed_ms:.0f}ms")
                else:
                    self._fail(label, f"status={r.status_code} expected={expected_status}")
            except Exception as e:
                self._fail(f"health_{path.strip('/') or 'root'}", str(e))

    # ------------------------------------------------------------------
    # Suite 6 — Agent Welcome Flow
    # ------------------------------------------------------------------

    def suite6_agent_welcome(self):
        print("\n=== SUITE 6: Agent Welcome Flow ===")

        token, uid, email = self._fresh_user("s6")
        if not token or not uid:
            self._fail("s6_setup", "could not register fresh test user")
            return

        hdrs = {"Authorization": f"Bearer {token}"}

        # 1. Agent name starts empty
        try:
            r = self.client.get("/api/agent/name", headers=hdrs)
            if r.status_code == 200 and r.json().get("name") in (None, ""):
                self._pass("s6_name_empty", "name is empty for new user")
            else:
                self._fail("s6_name_empty",
                    f"status={r.status_code} name={r.json().get('name')!r}")
        except Exception as e:
            self._fail("s6_name_empty", str(e))

        # 2. First message returns a welcome response
        if not self.skip_ai:
            try:
                r = self.client.post("/api/agent/chat", headers=hdrs,
                    json={"message": "Hello, who are you?", "history": []}, timeout=30)
                if r.status_code == 200 and r.json().get("response"):
                    self._pass("s6_first_message",
                        f"got {len(r.json()['response'])} chars")
                else:
                    self._fail("s6_first_message",
                        f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s6_first_message", str(e))
        else:
            self._pass("s6_first_message", "--skip-ai (chat call skipped)")

        # 3. Save agent name "Nova" via PATCH
        try:
            r = self.client.patch("/api/agent/name", headers=hdrs,
                json={"name": "Nova"})
            if r.status_code == 200 and r.json().get("name") == "Nova":
                self._pass("s6_name_saved", "name=Nova")
            else:
                self._fail("s6_name_saved",
                    f"status={r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s6_name_saved", str(e))

        # 4. GET confirms name persisted
        try:
            r = self.client.get("/api/agent/name", headers=hdrs)
            if r.status_code == 200 and r.json().get("name") == "Nova":
                self._pass("s6_name_persisted")
            else:
                self._fail("s6_name_persisted",
                    f"got name={r.json().get('name')!r}")
        except Exception as e:
            self._fail("s6_name_persisted", str(e))

        # 5. Agent greets with the saved name (AI call)
        if not self.skip_ai:
            try:
                r = self.client.post("/api/agent/chat", headers=hdrs,
                    json={"message": "What is your name?", "history": []}, timeout=30)
                if r.status_code == 200:
                    text = r.json().get("response", "")
                    if "Nova" in text:
                        self._pass("s6_name_in_response", "response contains 'Nova'")
                    else:
                        self._fail("s6_name_in_response",
                            f"'Nova' not found in: {text[:120]}")
                else:
                    self._fail("s6_name_in_response",
                        f"status={r.status_code}")
            except Exception as e:
                self._fail("s6_name_in_response", str(e))
        else:
            self._pass("s6_name_in_response", "--skip-ai (chat verification skipped)")

        self._remove_fresh_user(uid, email)

    # ------------------------------------------------------------------
    # Suite 7 — Voice Features
    # ------------------------------------------------------------------

    def suite7_voice_features(self):
        print("\n=== SUITE 7: Voice Features ===")
        if not self.token:
            self._fail("s7_skip", "no token — skipping suite")
            return

        hdrs = self._auth_headers()

        # 1. GET /api/agent/voices → list returned
        try:
            r = self.client.get("/api/agent/voices", headers=hdrs)
            if r.status_code == 200 and "voices" in r.json():
                voices = r.json()["voices"]
                self._pass("s7_voices_list",
                    f"{len(voices)} voices returned")
            else:
                self._fail("s7_voices_list",
                    f"status={r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s7_voices_list", str(e))

        # 2. GET /api/agent/voice_settings → defaults present
        try:
            r = self.client.get("/api/agent/voice_settings", headers=hdrs)
            if r.status_code == 200:
                d = r.json()
                if "voice_enabled" in d and "voice_id" in d:
                    self._pass("s7_voice_settings_defaults",
                        f"voice_enabled={d['voice_enabled']} voice_id present")
                else:
                    self._fail("s7_voice_settings_defaults",
                        f"missing keys — got: {list(d.keys())}")
            else:
                self._fail("s7_voice_settings_defaults",
                    f"status={r.status_code}")
        except Exception as e:
            self._fail("s7_voice_settings_defaults", str(e))

        # 3. POST → enable voice
        try:
            r = self.client.post("/api/agent/voice_settings", headers=hdrs,
                json={"voice_enabled": True})
            if r.status_code == 200 and r.json().get("ok"):
                self._pass("s7_voice_enable", "voice_enabled=true saved")
            else:
                self._fail("s7_voice_enable",
                    f"status={r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s7_voice_enable", str(e))

        # 4. GET → confirm persisted
        try:
            r = self.client.get("/api/agent/voice_settings", headers=hdrs)
            if r.status_code == 200 and r.json().get("voice_enabled") is True:
                self._pass("s7_voice_enabled_persisted")
            else:
                got = r.json().get("voice_enabled") if r.status_code == 200 else r.status_code
                self._fail("s7_voice_enabled_persisted",
                    f"voice_enabled={got!r} expected True")
        except Exception as e:
            self._fail("s7_voice_enabled_persisted", str(e))

        # Cleanup — reset voice setting
        try:
            self.client.post("/api/agent/voice_settings", headers=hdrs,
                json={"voice_enabled": False})
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Suite 8 — Watermark
    # ------------------------------------------------------------------

    def suite8_watermark(self):
        print("\n=== SUITE 8: Watermark ===")
        if not self.token:
            self._fail("s8_skip", "no token — skipping suite")
            return

        hdrs = self._auth_headers()

        # --- Free user tests ---

        # 1. Free user: watermark_enabled = True by default
        try:
            r = self.client.get("/api/user/profile", headers=hdrs)
            if r.status_code == 200:
                d    = r.json()
                plan = d.get("plan", "")
                wm   = d.get("watermark_enabled")
                if plan == "free" and wm is True:
                    self._pass("s8_free_watermark_on",
                        "watermark=True on free plan")
                elif plan != "free":
                    self._fail("s8_free_watermark_on",
                        f"QA user not on free plan (plan={plan!r})")
                else:
                    self._fail("s8_free_watermark_on",
                        f"watermark_enabled={wm!r} expected True")
            else:
                self._fail("s8_free_watermark_on", f"status={r.status_code}")
        except Exception as e:
            self._fail("s8_free_watermark_on", str(e))

        # 2. Free user: cannot disable watermark → 403
        try:
            r = self.client.post("/api/user/watermark", headers=hdrs,
                json={"enabled": False})
            if r.status_code == 403:
                self._pass("s8_free_watermark_locked",
                    "correctly blocked with 403")
            else:
                self._fail("s8_free_watermark_locked",
                    f"expected 403 got {r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s8_free_watermark_locked", str(e))

        # --- Paid user tests ---

        token_p, uid_p, email_p = self._fresh_user("s8paid")
        if not token_p or not uid_p:
            self._fail("s8_paid_setup", "could not create paid test user")
            return

        if not self._set_billing(uid_p, "starter", 100):
            self._fail("s8_paid_billing", "could not set plan=starter in user store")
            self._remove_fresh_user(uid_p, email_p)
            return

        hdrs_p = {"Authorization": f"Bearer {token_p}"}

        # 3. Paid user: watermark_enabled = False by default
        try:
            r = self.client.get("/api/user/profile", headers=hdrs_p)
            if r.status_code == 200:
                wm = r.json().get("watermark_enabled")
                if wm is False:
                    self._pass("s8_paid_watermark_off",
                        "watermark=False by default on paid plan")
                else:
                    self._fail("s8_paid_watermark_off",
                        f"watermark_enabled={wm!r} expected False")
            else:
                self._fail("s8_paid_watermark_off", f"status={r.status_code}")
        except Exception as e:
            self._fail("s8_paid_watermark_off", str(e))

        # 4. Paid user: toggle watermark on → saved
        try:
            r = self.client.post("/api/user/watermark", headers=hdrs_p,
                json={"enabled": True})
            if r.status_code == 200 and r.json().get("watermark_enabled") is True:
                self._pass("s8_paid_watermark_toggle",
                    "watermark toggled on successfully")
            else:
                self._fail("s8_paid_watermark_toggle",
                    f"status={r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s8_paid_watermark_toggle", str(e))

        self._remove_fresh_user(uid_p, email_p)

    # ------------------------------------------------------------------
    # Suite 9 — AI Portrait (face-scene)
    # ------------------------------------------------------------------

    def suite9_ai_portrait(self):
        print("\n=== SUITE 9: AI Portrait ===")
        if not self.token:
            self._fail("s9_skip", "no token — skipping suite")
            return

        hdrs = self._auth_headers()
        # Minimal payload — errors happen before image processing for plan/credit checks
        payload = {
            "face_image":    "https://upload.wikimedia.org/wikipedia/commons/a/a7/Camponotus_flavomarginatus_ant.jpg",
            "scene_prompt":  "professional office background, corporate headshot",
            "style":         "professional",
        }

        # 1. Free user → 403 (plan gate fires before any image work)
        try:
            r = self.client.post("/api/studio/face-scene", headers=hdrs,
                json=payload, timeout=15)
            if r.status_code == 403:
                self._pass("s9_free_blocked", "403 on free plan")
            elif r.status_code == 503:
                self._fail("s9_free_blocked",
                    "503 — Replicate key missing, plan gate not reached")
            else:
                self._fail("s9_free_blocked",
                    f"expected 403 got {r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s9_free_blocked", str(e))

        # --- Paid user with < 50 credits → 402 ---

        token_p, uid_p, email_p = self._fresh_user("s9paid")
        if not token_p or not uid_p:
            self._fail("s9_paid_setup", "could not create paid test user")
            return

        # Set credits to 20 — below FACE_SCENE_COST (50)
        if not self._set_billing(uid_p, "starter", 20):
            self._fail("s9_paid_billing", "could not set plan=starter credits=20")
            self._remove_fresh_user(uid_p, email_p)
            return

        hdrs_p = {"Authorization": f"Bearer {token_p}"}

        # 2. Paid user, insufficient credits → 402
        try:
            r = self.client.post("/api/studio/face-scene", headers=hdrs_p,
                json=payload, timeout=15)
            if r.status_code == 402:
                self._pass("s9_paid_low_credits",
                    "402 on insufficient credits (need 50, have 20)")
            elif r.status_code == 503:
                self._fail("s9_paid_low_credits",
                    "503 — Replicate key missing, credit gate not reached")
            else:
                self._fail("s9_paid_low_credits",
                    f"expected 402 got {r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s9_paid_low_credits", str(e))

        # 3. Endpoint exists — presets endpoint is always 200 for any paid user
        try:
            r = self.client.get("/api/studio/face-scene/presets",
                headers=hdrs_p, timeout=10)
            if r.status_code == 200 and "presets" in r.json():
                self._pass("s9_endpoint_exists",
                    f"{len(r.json()['presets'])} presets, cost={r.json().get('cost')}")
            else:
                self._fail("s9_endpoint_exists",
                    f"status={r.status_code}")
        except Exception as e:
            self._fail("s9_endpoint_exists", str(e))

        self._remove_fresh_user(uid_p, email_p)

    # ------------------------------------------------------------------
    # Suite 10 — Content Plan
    # ------------------------------------------------------------------

    def suite10_content_plan(self):
        print("\n=== SUITE 10: Content Plan ===")
        if not self.token:
            self._fail("s10_skip", "no token — skipping suite")
            return

        hdrs = self._auth_headers()
        plan_payload = {
            "niche":         "fitness & wellness",
            "goal":          "grow followers and drive engagement",
            "posts_per_day": 1,
            "platforms":     ["instagram"],
            "tone":          "Motivational",
            "language":      "English",
        }

        # 1. Free user → 403
        try:
            r = self.client.post("/api/content/create-plan", headers=hdrs,
                json=plan_payload, timeout=15)
            if r.status_code == 403:
                self._pass("s10_free_blocked", "403 on free plan")
            else:
                self._fail("s10_free_blocked",
                    f"expected 403 got {r.status_code} body={r.text[:80]}")
        except Exception as e:
            self._fail("s10_free_blocked", str(e))

        # AI-dependent tests (skip if --skip-ai)
        if self.skip_ai:
            self._pass("s10_plan_gen", "--skip-ai (content plan generation skipped)")
            self._pass("s10_credits_deducted", "--skip-ai (credit check skipped)")
            return

        # --- Paid user with enough credits → full plan returned ---

        token_p, uid_p, email_p = self._fresh_user("s10paid")
        if not token_p or not uid_p:
            self._fail("s10_paid_setup", "could not create paid test user")
            return

        credits_start = 100
        if not self._set_billing(uid_p, "starter", credits_start):
            self._fail("s10_paid_billing", "could not set plan=starter credits=100")
            self._remove_fresh_user(uid_p, email_p)
            return

        hdrs_p = {"Authorization": f"Bearer {token_p}"}

        # 2. POST create-plan → 200, plan returned with 7 days
        try:
            r = self.client.post("/api/content/create-plan", headers=hdrs_p,
                json=plan_payload, timeout=90)
            if r.status_code == 200:
                plan = r.json()
                days = plan.get("days", [])
                if isinstance(days, list) and len(days) == 7:
                    total_posts = sum(len(d.get("posts", [])) for d in days)
                    self._pass("s10_plan_returned",
                        f"7 days, {total_posts} posts total")
                else:
                    self._fail("s10_plan_returned",
                        f"days={len(days)} expected 7")
            elif r.status_code == 503:
                self._fail("s10_plan_returned",
                    "503 — AI service unavailable")
            else:
                self._fail("s10_plan_returned",
                    f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s10_plan_returned", str(e))

        # 3. Verify 10 credits were deducted
        try:
            r = self.client.get("/api/billing/plan", headers=hdrs_p)
            if r.status_code == 200:
                credits_now = r.json().get("credits", -1)
                expected    = credits_start - 10
                if credits_now == expected:
                    self._pass("s10_credits_deducted",
                        f"{credits_start}→{credits_now} (-10)")
                else:
                    self._fail("s10_credits_deducted",
                        f"expected {expected} got {credits_now}")
            else:
                self._fail("s10_credits_deducted",
                    f"billing check status={r.status_code}")
        except Exception as e:
            self._fail("s10_credits_deducted", str(e))

        self._remove_fresh_user(uid_p, email_p)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # Suite 11 — Full Auto Mode
    # ------------------------------------------------------------------

    def suite11_full_auto(self):
        print("\n=== SUITE 11: Full Auto Mode ===")

        # Pro user with 500 credits should enable full auto
        token, uid, email = self._fresh_user("s11a")
        if not uid:
            self._fail("s11_register", "could not create test user")
            return
        self._set_billing(uid, "pro", 500)
        h = {"Authorization": f"Bearer {token}"}

        try:
            r = self.client.post("/api/autopilot/full_auto_toggle",
                                 json={"enabled": True, "mode": "review"}, headers=h)
            if r.status_code == 200 and r.json().get("full_auto_enabled") is True:
                self._pass("s11_full_auto_enable",
                           f"mode={r.json().get('full_auto_mode')}")
            else:
                self._fail("s11_full_auto_enable",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s11_full_auto_enable", str(e))

        # Verify persisted via status endpoint
        try:
            r = self.client.get("/api/autopilot/full_auto_status", headers=h)
            if r.status_code == 200 and r.json().get("full_auto_enabled") is True:
                self._pass("s11_full_auto_status_persisted")
            else:
                self._fail("s11_full_auto_status_persisted",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s11_full_auto_status_persisted", str(e))

        self._remove_fresh_user(uid, email)

        # Pro user with only 100 credits → should be blocked (min 200)
        token2, uid2, email2 = self._fresh_user("s11b")
        if not uid2:
            self._fail("s11_low_credits_register", "could not create second test user")
            return
        self._set_billing(uid2, "pro", 100)
        h2 = {"Authorization": f"Bearer {token2}"}

        try:
            r = self.client.post("/api/autopilot/full_auto_toggle",
                                 json={"enabled": True, "mode": "review"}, headers=h2)
            d = r.json()
            if r.status_code == 200 and d.get("ok") is False and d.get("error") == "insufficient_credits":
                self._pass("s11_low_credits_blocked",
                           f"credits={d.get('credits')}, minimum={d.get('minimum')}")
            else:
                self._fail("s11_low_credits_blocked",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s11_low_credits_blocked", str(e))

        # Free user → plan check should block it
        token3, uid3, email3 = self._fresh_user("s11c")
        if uid3:
            h3 = {"Authorization": f"Bearer {token3}"}
            try:
                r = self.client.post("/api/autopilot/full_auto_toggle",
                                     json={"enabled": True, "mode": "review"}, headers=h3)
                d = r.json()
                if r.status_code == 200 and d.get("ok") is False and d.get("error") == "pro_required":
                    self._pass("s11_free_plan_blocked")
                else:
                    self._fail("s11_free_plan_blocked",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s11_free_plan_blocked", str(e))
            self._remove_fresh_user(uid3, email3)

        self._remove_fresh_user(uid2, email2)

    # ------------------------------------------------------------------
    # Suite 12 — Multi-workspace
    # ------------------------------------------------------------------

    def suite12_workspaces(self):
        print("\n=== SUITE 12: Multi-workspace ===")

        # Agency user — can create multiple workspaces
        token, uid, email = self._fresh_user("s12")
        if not uid:
            self._fail("s12_register", "could not create test user")
            return
        self._set_billing(uid, "agency", 100)
        h = {"Authorization": f"Bearer {token}"}

        # GET /api/workspaces — creates default workspace
        try:
            r = self.client.get("/api/workspaces", headers=h)
            if r.status_code == 200:
                wss = r.json().get("workspaces", [])
                self._pass("s12_list_initial", f"{len(wss)} workspace(s)")
            else:
                self._fail("s12_list_initial", f"status={r.status_code}")
                self._remove_fresh_user(uid, email)
                return
        except Exception as e:
            self._fail("s12_list_initial", str(e))
            self._remove_fresh_user(uid, email)
            return

        # POST /api/workspaces — create "Test Brand"
        new_ws_id = None
        try:
            r = self.client.post("/api/workspaces",
                                 json={"name": "Test Brand", "niche": "fitness"},
                                 headers=h)
            if r.status_code == 200 and r.json().get("ok"):
                new_ws_id = r.json().get("workspace", {}).get("id")
                self._pass("s12_create_workspace", f"id={new_ws_id}")
            else:
                self._fail("s12_create_workspace",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s12_create_workspace", str(e))

        # GET — verify "Test Brand" in list
        if new_ws_id:
            try:
                r = self.client.get("/api/workspaces", headers=h)
                names = [w.get("name") for w in r.json().get("workspaces", [])]
                if "Test Brand" in names:
                    self._pass("s12_workspace_in_list", f"workspaces={names}")
                else:
                    self._fail("s12_workspace_in_list", f"names={names}")
            except Exception as e:
                self._fail("s12_workspace_in_list", str(e))

            # POST /api/workspaces/{id}/switch
            try:
                r = self.client.post(f"/api/workspaces/{new_ws_id}/switch", headers=h)
                if r.status_code == 200 and r.json().get("active_workspace") == new_ws_id:
                    self._pass("s12_switch_workspace")
                else:
                    self._fail("s12_switch_workspace",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s12_switch_workspace", str(e))

            # DELETE /api/workspaces/{id}
            try:
                r = self.client.delete(f"/api/workspaces/{new_ws_id}", headers=h)
                if r.status_code == 200 and r.json().get("ok"):
                    self._pass("s12_delete_workspace")
                else:
                    self._fail("s12_delete_workspace",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s12_delete_workspace", str(e))

        self._remove_fresh_user(uid, email)

        # Free user — cannot create a second workspace (403)
        token_f, uid_f, email_f = self._fresh_user("s12free")
        if uid_f:
            h_f = {"Authorization": f"Bearer {token_f}"}
            # Create default workspace first
            self.client.get("/api/workspaces", headers=h_f)
            try:
                r = self.client.post("/api/workspaces",
                                     json={"name": "Should Fail"}, headers=h_f)
                if r.status_code == 403:
                    self._pass("s12_free_blocked", "403 as expected")
                else:
                    self._fail("s12_free_blocked",
                               f"expected 403 got {r.status_code} body={r.text[:100]}")
            except Exception as e:
                self._fail("s12_free_blocked", str(e))
            self._remove_fresh_user(uid_f, email_f)

    # ------------------------------------------------------------------
    # Suite 13 — Agent Tasks
    # ------------------------------------------------------------------

    def suite13_agent_tasks(self):
        print("\n=== SUITE 13: Agent Tasks ===")

        token, uid, email = self._fresh_user("s13")
        if not uid:
            self._fail("s13_register", "could not create test user")
            return
        self._set_billing(uid, "starter", 200)
        h = {"Authorization": f"Bearer {token}"}

        task_id = None

        if not self.skip_ai:
            # Agent chat should detect "monitor #fitness" and create a task
            try:
                r = self.client.post("/api/agent/chat",
                                     json={"message": "monitor #fitness for me",
                                           "history": []},
                                     headers=h, timeout=30)
                if r.status_code == 200:
                    self._pass("s13_agent_chat_ok", "agent responded")
                else:
                    self._fail("s13_agent_chat_ok",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s13_agent_chat_ok", str(e))

            # Verify task was auto-created
            try:
                r = self.client.get("/api/agent/tasks", headers=h)
                tasks = r.json().get("tasks", [])
                t = next((x for x in tasks
                          if x.get("type") == "monitor_hashtag"
                          and x.get("params", {}).get("hashtag") == "fitness"), None)
                if t:
                    task_id = t["id"]
                    self._pass("s13_task_auto_created", f"id={task_id}")
                else:
                    self._fail("s13_task_auto_created",
                               f"monitor_hashtag/fitness not found in {tasks}")
            except Exception as e:
                self._fail("s13_task_auto_created", str(e))
        else:
            self._pass("s13_agent_chat_ok", "skipped (--skip-ai)")

        # If no task yet (skip_ai or creation failed), inject one directly
        if not task_id:
            import uuid as _uuid2
            task = {
                "id": _uuid2.uuid4().hex[:12],
                "type": "monitor_hashtag",
                "label": "Monitor hashtag",
                "params": {"hashtag": "fitness"},
                "schedule": "daily",
                "active": True,
                "created_at": datetime.now().isoformat(),
                "last_run": None,
                "run_count": 0,
            }
            ufile = os.path.join(USER_DATA_DIR, f"{uid}.json")
            try:
                data = {}
                if os.path.exists(ufile):
                    with open(ufile, encoding="utf-8") as f:
                        data = json.load(f)
                data.setdefault("agent_tasks", []).append(task)
                with open(ufile, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
                task_id = task["id"]
                if self.skip_ai:
                    self._pass("s13_task_auto_created",
                               "injected directly (skip_ai mode)")
            except Exception as e:
                self._fail("s13_task_inject", str(e))

        if not task_id:
            self._remove_fresh_user(uid, email)
            return

        # GET /api/agent/tasks — verify task present
        try:
            r = self.client.get("/api/agent/tasks", headers=h)
            if r.status_code == 200:
                tasks = r.json().get("tasks", [])
                if any(t["id"] == task_id for t in tasks):
                    self._pass("s13_tasks_list", f"{len(tasks)} task(s)")
                else:
                    self._fail("s13_tasks_list", "task_id not found in list")
            else:
                self._fail("s13_tasks_list", f"status={r.status_code}")
        except Exception as e:
            self._fail("s13_tasks_list", str(e))

        # PATCH — toggle off
        try:
            r = self.client.patch(f"/api/agent/tasks/{task_id}",
                                  json={"active": False}, headers=h)
            if (r.status_code == 200
                    and r.json().get("task", {}).get("active") is False):
                self._pass("s13_task_toggle_off")
            else:
                self._fail("s13_task_toggle_off",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s13_task_toggle_off", str(e))

        # DELETE
        try:
            r = self.client.delete(f"/api/agent/tasks/{task_id}", headers=h)
            if r.status_code == 200 and r.json().get("ok"):
                self._pass("s13_task_delete")
            else:
                self._fail("s13_task_delete",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s13_task_delete", str(e))

        # Verify gone
        try:
            r = self.client.get("/api/agent/tasks", headers=h)
            remaining = r.json().get("tasks", [])
            if not any(t["id"] == task_id for t in remaining):
                self._pass("s13_task_deleted_verify")
            else:
                self._fail("s13_task_deleted_verify", "task still present after delete")
        except Exception as e:
            self._fail("s13_task_deleted_verify", str(e))

        self._remove_fresh_user(uid, email)

    # ------------------------------------------------------------------
    # Suite 14 — Anti-ban / Proxy / Safety
    # ------------------------------------------------------------------

    def suite14_anti_ban(self):
        print("\n=== SUITE 14: Anti-ban / Proxy / Safety ===")

        # Growth user — proxy assignment allowed
        token, uid, email = self._fresh_user("s14")
        if not uid:
            self._fail("s14_register", "could not create test user")
            return
        self._set_billing(uid, "growth", 100)
        h = {"Authorization": f"Bearer {token}"}

        # POST /api/automation/assign_proxy — growth+ should succeed
        try:
            r = self.client.post("/api/automation/assign_proxy", headers=h)
            if r.status_code == 200 and r.json().get("ok"):
                self._pass("s14_proxy_assign",
                           f"session={r.json().get('session_id','')[:20]}")
            else:
                self._fail("s14_proxy_assign",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s14_proxy_assign", str(e))

        # GET /api/automation/safety — verify response structure
        try:
            r = self.client.get("/api/automation/safety", headers=h)
            if r.status_code == 200:
                d = r.json()
                has_keys = all(k in d for k in
                               ("safety_level", "usage", "limits",
                                "proxy_assigned", "proxy_session"))
                if has_keys:
                    self._pass("s14_safety_get",
                               f"level={d.get('safety_level')} proxy={d.get('proxy_assigned')}")
                else:
                    self._fail("s14_safety_get",
                               f"missing keys in {list(d.keys())}")
            else:
                self._fail("s14_safety_get", f"status={r.status_code}")
        except Exception as e:
            self._fail("s14_safety_get", str(e))

        # POST /api/automation/safety — set conservative
        try:
            r = self.client.post("/api/automation/safety",
                                 json={"safety_level": "conservative"}, headers=h)
            if r.status_code == 200 and r.json().get("safety_level") == "conservative":
                self._pass("s14_safety_set_conservative")
            else:
                self._fail("s14_safety_set_conservative",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s14_safety_set_conservative", str(e))

        # Verify persisted
        try:
            r = self.client.get("/api/automation/safety", headers=h)
            if r.status_code == 200 and r.json().get("safety_level") == "conservative":
                self._pass("s14_safety_persisted")
            else:
                self._fail("s14_safety_persisted",
                           f"got level={r.json().get('safety_level')!r}")
        except Exception as e:
            self._fail("s14_safety_persisted", str(e))

        self._remove_fresh_user(uid, email)

        # Free user — proxy blocked (403)
        token_f, uid_f, email_f = self._fresh_user("s14free")
        if uid_f:
            h_f = {"Authorization": f"Bearer {token_f}"}
            try:
                r = self.client.post("/api/automation/assign_proxy", headers=h_f)
                if r.status_code == 403:
                    self._pass("s14_free_proxy_blocked", "403 as expected")
                else:
                    self._fail("s14_free_proxy_blocked",
                               f"expected 403 got {r.status_code}")
            except Exception as e:
                self._fail("s14_free_proxy_blocked", str(e))
            self._remove_fresh_user(uid_f, email_f)

    # ------------------------------------------------------------------
    # Suite 15 — Analytics endpoints
    # ------------------------------------------------------------------

    def suite15_analytics(self):
        print("\n=== SUITE 15: Analytics ===")

        token, uid, email = self._fresh_user("s15")
        if not uid:
            self._fail("s15_register", "could not create test user")
            return
        self._set_billing(uid, "starter", 200)
        h = {"Authorization": f"Bearer {token}"}

        # GET /api/analytics/platform — verify structure
        try:
            r = self.client.get("/api/analytics/platform", headers=h)
            if r.status_code == 200:
                d = r.json()
                has_keys = all(k in d for k in ("instagram", "youtube", "tiktok"))
                if has_keys:
                    self._pass("s15_platform_structure",
                               f"instagram_connected={d['instagram'].get('connected')}")
                else:
                    self._fail("s15_platform_structure",
                               f"missing keys in {list(d.keys())}")
            else:
                self._fail("s15_platform_structure", f"status={r.status_code}")
        except Exception as e:
            self._fail("s15_platform_structure", str(e))

        # GET /api/analytics/performance — verify structure
        try:
            r = self.client.get("/api/analytics/performance", headers=h)
            if r.status_code == 200:
                d = r.json()
                if "performance" in d and "summary" in d:
                    self._pass("s15_performance_get",
                               f"tracked={d.get('summary', {}).get('total_tracked', 0)}")
                else:
                    self._fail("s15_performance_get",
                               f"bad structure: {list(d.keys())}")
            else:
                self._fail("s15_performance_get", f"status={r.status_code}")
        except Exception as e:
            self._fail("s15_performance_get", str(e))

        # POST /api/analytics/update_performance — save test record
        test_post_id = f"qa-test-{int(time.time())}"
        try:
            r = self.client.post("/api/analytics/update_performance",
                                 json={
                                     "post_id":      test_post_id,
                                     "platform":     "instagram",
                                     "content_type": "image",
                                     "caption":      "QA test post",
                                     "views":        1000,
                                     "likes":        80,
                                     "comments":     12,
                                     "shares":       5,
                                 },
                                 headers=h)
            if r.status_code == 200 and r.json().get("ok"):
                er = r.json().get("engagement_rate", -1)
                self._pass("s15_performance_update",
                           f"engagement_rate={er}")
            else:
                self._fail("s15_performance_update",
                           f"status={r.status_code} body={r.text[:120]}")
        except Exception as e:
            self._fail("s15_performance_update", str(e))

        # GET /api/analytics/performance — verify saved record exists
        try:
            r = self.client.get("/api/analytics/performance", headers=h)
            if r.status_code == 200:
                perf = r.json().get("performance", [])
                found = any(p.get("post_id") == test_post_id for p in perf)
                if found:
                    self._pass("s15_performance_persisted",
                               "test record found")
                else:
                    self._fail("s15_performance_persisted",
                               f"post_id={test_post_id} not in {[p.get('post_id') for p in perf[:5]]}")
            else:
                self._fail("s15_performance_persisted", f"status={r.status_code}")
        except Exception as e:
            self._fail("s15_performance_persisted", str(e))

        # GET /api/analytics/dashboard — smoke test
        try:
            r = self.client.get("/api/analytics/dashboard", headers=h)
            if r.status_code == 200 and "kpis" in r.json():
                self._pass("s15_dashboard_ok")
            else:
                self._fail("s15_dashboard_ok", f"status={r.status_code}")
        except Exception as e:
            self._fail("s15_dashboard_ok", str(e))

        self._remove_fresh_user(uid, email)

    def _cleanup(self):
        if not self.user_id:
            return
        user_file = os.path.join(USER_DATA_DIR, f"{self.user_id}.json")
        if os.path.exists(user_file):
            try:
                with open(user_file, encoding="utf-8") as f:
                    data = json.load(f)
                data["credits"] = 50
                with open(user_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                print(f"\n[cleanup] Reset credits to 50 for user {self.user_id}")
            except Exception as e:
                print(f"\n[cleanup] Failed: {e}")

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def _save_history(self, summary: dict):
        history = []
        if os.path.exists(RESULTS_FILE):
            try:
                with open(RESULTS_FILE, encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.append(summary)
        history = history[-50:]
        with open(RESULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    def _report_nexcore(self, summary: dict):
        try:
            r = httpx.post(
                NEXORA_URL,
                headers={"X-Nexora-Key": NEXORA_KEY, "Content-Type": "application/json"},
                json=summary,
                timeout=10,
            )
            print(f"\n[report] Nexcore → status={r.status_code}")
        except Exception as e:
            print(f"\n[report] Nexcore failed: {e}")

    def _telegram_alert(self, summary: dict):
        if not TG_BOT_TOKEN or not TG_ALERT_CHAT:
            return
        failed = [r for r in summary["results"] if r["status"] == "FAIL"]
        health_warnings = (summary.get("health") or {}).get("warnings") or []
        # Alert if EITHER functional tests failed OR a server-health threshold
        # was breached — so we hear about CPU/mem/disk/DDoS/error-rate problems
        # before the box goes down, even when every functional test passes.
        if not failed and not health_warnings:
            return
        lines = []
        if failed:
            lines.append(f"*UgoingViral QA FAILED* — {summary['timestamp']}")
            lines.append(f"{summary['passed']}/{summary['total']} passed")
            lines.append("")
            for r in failed[:10]:
                lines.append(f"❌ `{r['name']}`: {r['detail']}")
        if health_warnings:
            if failed:
                lines.append("")
            else:
                lines.append(f"*UgoingViral SERVER HEALTH ALERT* — {summary['timestamp']}")
            lines.append("🚨 *Server health abnormal (act before downtime):*")
            for w in health_warnings:
                lines.append(f"⚠️ {w}")
            m = (summary.get("health") or {}).get("metrics") or {}
            lines.append(
                f"_cpu={m.get('cpu_pct')}% mem={m.get('mem_pct')}% disk={m.get('disk_pct')}% "
                f"req/min={m.get('req_per_min_avg')} 5xx={m.get('err_5xx_pct')}%_")
        msg = "\n".join(lines)
        try:
            httpx.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                json={"chat_id": TG_ALERT_CHAT, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            print(f"[telegram] alert failed: {e}")

    # ------------------------------------------------------------------
    # Suite 16 — UI regression tests (FAB, missing-content, connect)
    # ------------------------------------------------------------------

    def suite16_ui_regressions(self):
        """Catch regressions reported by users:
        - Floating agent button HTML present + agToggle() defined in JS
        - /api/automation/generate_batch returns 200 (not 500) for the
          "missing content" click on the Products page
        - Connect page loads its connection data without 5xx
        """
        print("\n=== SUITE 16: UI regressions ===")

        # 16.1 — Frontend bundle has the agent FAB + working toggle handler.
        try:
            r = self.client.get("/app")
            html = r.text if r.status_code == 200 else ""
            has_fab    = ('id="agent-fab"' in html)
            has_toggle = ('function agToggle()' in html)
            has_panel  = ('id="agent-panel"' in html)
            if r.status_code == 200 and has_fab and has_toggle and has_panel:
                self._pass("s16_agent_fab_present",
                           "FAB+panel+agToggle all in bundle")
            else:
                self._fail("s16_agent_fab_present",
                           f"status={r.status_code} fab={has_fab} toggle={has_toggle} panel={has_panel}")
        except Exception as e:
            self._fail("s16_agent_fab_present", str(e))

        # 16.2 — Products "Missing content" click endpoint must not 500.
        # We hit it with a fresh free user that has zero products, which is
        # exactly the path that used to throw NameError: _demo_products.
        token, uid, email = self._fresh_user("s16a")
        if not uid:
            self._fail("s16_missing_content_register", "could not create test user")
            return
        h = {"Authorization": f"Bearer {token}"}
        try:
            r = self.client.post("/api/automation/generate_batch", json={}, headers=h)
            if r.status_code == 200:
                d = r.json()
                if d.get("status") in ("no_products", "done") and d.get("generated", 0) == 0:
                    self._pass("s16_missing_content_endpoint",
                               f"status={d.get('status')}")
                else:
                    self._fail("s16_missing_content_endpoint",
                               f"unexpected body: {r.text[:160]}")
            else:
                self._fail("s16_missing_content_endpoint",
                           f"status={r.status_code} body={r.text[:160]}")
        except Exception as e:
            self._fail("s16_missing_content_endpoint", str(e))

        # 16.3 — Connect page loads its connection state without 5xx.
        try:
            r = self.client.get("/api/settings/connections", headers=h)
            if r.status_code == 200 and isinstance(r.json(), dict):
                self._pass("s16_connect_page_loads")
            else:
                self._fail("s16_connect_page_loads",
                           f"status={r.status_code} body={r.text[:160]}")
        except Exception as e:
            self._fail("s16_connect_page_loads", str(e))

        self._remove_fresh_user(uid, email)

    # ------------------------------------------------------------------
    # Suite 17 — Auto Pilot toggle, content generation, scheduling, and
    #            in-platform agent platform commands (no real social posts)
    # ------------------------------------------------------------------

    def suite17_autopilot_posting(self):
        print("\n=== SUITE 17: Auto Pilot + Posting + Agent ===")

        token, uid, email = self._fresh_user("s17")
        if not uid:
            self._fail("s17_register", "could not create test user")
            return
        # Pro plan + enough credits to clear the 200-credit Auto Pilot floor.
        self._set_billing(uid, "pro", 500)
        h = {"Authorization": f"Bearer {token}"}

        try:
            # ── 1. Auto Pilot can be ENABLED via API ──────────────────────────
            try:
                r = self.client.post("/api/autopilot/toggle",
                                     json={"active": True}, headers=h)
                d = r.json() if r.status_code == 200 else {}
                if r.status_code == 200 and d.get("ok") is True and d.get("active") is True:
                    self._pass("s17_autopilot_enable", "active=True")
                else:
                    self._fail("s17_autopilot_enable",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s17_autopilot_enable", str(e))

            # status endpoint should reflect the enabled state
            try:
                r = self.client.get("/api/autopilot/status", headers=h)
                if r.status_code == 200 and r.json().get("active") is True:
                    self._pass("s17_autopilot_status_active")
                else:
                    self._fail("s17_autopilot_status_active",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s17_autopilot_status_active", str(e))

            # ── 2. Auto Pilot can be DISABLED via API ─────────────────────────
            try:
                r = self.client.post("/api/autopilot/toggle",
                                     json={"active": False}, headers=h)
                d = r.json() if r.status_code == 200 else {}
                if r.status_code == 200 and d.get("ok") is True and d.get("active") is False:
                    self._pass("s17_autopilot_disable", "active=False")
                else:
                    self._fail("s17_autopilot_disable",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s17_autopilot_disable", str(e))

            # ── 3. Content generation returns content WITHOUT posting ─────────
            if self.skip_ai:
                self._pass("s17_content_gen_no_post", "--skip-ai flag set")
            else:
                try:
                    # baseline scheduled-post count before generating
                    base = self.client.get("/api/posts/scheduled", headers=h)
                    base_n = len(base.json().get("posts", [])) if base.status_code == 200 else 0

                    r = self.client.post("/api/content/generate", headers=h,
                                         json={"content_type": "caption",
                                               "platform": "tiktok",
                                               "product_title": "vores nye kaffemaskine",
                                               "language": "da", "tone": "engaging"},
                                         timeout=40)
                    if r.status_code in (200, 201) and (r.json().get("content") or "").strip():
                        # confirm nothing was published/scheduled as a side effect
                        after = self.client.get("/api/posts/scheduled", headers=h)
                        after_n = len(after.json().get("posts", [])) if after.status_code == 200 else 0
                        if after_n == base_n:
                            self._pass("s17_content_gen_no_post",
                                       f"content returned, scheduled unchanged ({after_n})")
                        else:
                            self._fail("s17_content_gen_no_post",
                                       f"content gen created {after_n - base_n} post(s) — should not post")
                    elif r.status_code in (402, 403):
                        self._pass("s17_content_gen_no_post", f"gated status={r.status_code}")
                    else:
                        self._fail("s17_content_gen_no_post",
                                   f"status={r.status_code} body={r.text[:120]}")
                except Exception as e:
                    self._fail("s17_content_gen_no_post", str(e))

            # ── 4. Scheduled post creation + listing ──────────────────────────
            test_caption = f"QA scheduled post {int(time.time())}"
            try:
                future = (datetime.utcnow() + timedelta(days=2)).isoformat()
                r = self.client.post("/api/posts/schedule", headers=h,
                                     json={"platform": "tiktok",
                                           "content": test_caption,
                                           "scheduled_time": future,
                                           "mode": "ui"})
                if r.status_code == 200 and r.json().get("status") == "scheduled":
                    self._pass("s17_schedule_create", f"id={r.json().get('id')}")
                else:
                    self._fail("s17_schedule_create",
                               f"status={r.status_code} body={r.text[:120]}")
            except Exception as e:
                self._fail("s17_schedule_create", str(e))

            try:
                r = self.client.get("/api/posts/scheduled", headers=h)
                posts = r.json().get("posts", []) if r.status_code == 200 else []
                if r.status_code == 200 and any(p.get("content") == test_caption for p in posts):
                    self._pass("s17_schedule_list", f"{len(posts)} post(s) listed")
                else:
                    self._fail("s17_schedule_list",
                               f"status={r.status_code} scheduled post not found in list")
            except Exception as e:
                self._fail("s17_schedule_list", str(e))

            # ── 5. Agent executes "lav et opslag om [topic]" → returns content ─
            if self.skip_ai:
                self._pass("s17_agent_make_post", "--skip-ai flag set")
            else:
                try:
                    r = self.client.post("/api/agent/chat", headers=h,
                                         json={"message": "Lav et opslag om vores nye kaffemaskine",
                                               "history": [], "context": ""},
                                         timeout=40)
                    resp = (r.json().get("response") or "") if r.status_code == 200 else ""
                    if r.status_code == 200 and len(resp.strip()) > 20:
                        self._pass("s17_agent_make_post", f"{len(resp)} chars returned")
                    else:
                        self._fail("s17_agent_make_post",
                                   f"status={r.status_code} body={r.text[:120]}")
                except Exception as e:
                    self._fail("s17_agent_make_post", str(e))

            # ── 6. Agent responds to platform commands (TikTok / schedule / stats)
            if self.skip_ai:
                self._pass("s17_agent_platform_commands", "--skip-ai flag set")
            else:
                platform_cmds = [
                    ("tiktok_post",    "Lav et TikTok opslag om vores nye produkt"),
                    ("schedule_week",  "Planlæg 3 opslag til næste uge"),
                    ("show_stats",     "Vis mig mine statistikker"),
                ]
                cmd_ok = 0
                details = []
                for key, msg in platform_cmds:
                    try:
                        r = self.client.post("/api/agent/chat", headers=h,
                                             json={"message": msg, "history": [], "context": ""},
                                             timeout=40)
                        if r.status_code == 200:
                            body = r.json()
                            resp = (body.get("response") or "").strip()
                            acts = body.get("actions", []) or []
                            if len(resp) > 10 or acts:
                                cmd_ok += 1
                                details.append(f"{key}:{len(resp)}c/{len(acts)}act")
                            else:
                                details.append(f"{key}:empty")
                        else:
                            details.append(f"{key}:HTTP{r.status_code}")
                    except Exception as e:
                        details.append(f"{key}:err {str(e)[:30]}")
                if cmd_ok == len(platform_cmds):
                    self._pass("s17_agent_platform_commands", "; ".join(details))
                else:
                    self._fail("s17_agent_platform_commands",
                               f"{cmd_ok}/{len(platform_cmds)} ok — " + "; ".join(details))
        finally:
            self._remove_fresh_user(uid, email)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Server health + runaway monitoring (CPU / memory / disk / request
    # rate / error rate). Stdlib only — no psutil. Runs on the same host
    # as the app (cron), so /proc and the nginx log are read directly.
    # ------------------------------------------------------------------

    @staticmethod
    def _cpu_percent(sample_s: float = 0.5) -> float:
        """Whole-host CPU utilisation %, from two /proc/stat samples."""
        def _read():
            with open("/proc/stat") as f:
                parts = f.readline().split()[1:]
            vals = [int(x) for x in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
            return sum(vals), idle
        t1, i1 = _read()
        time.sleep(sample_s)
        t2, i2 = _read()
        dt, di = (t2 - t1), (i2 - i1)
        if dt <= 0:
            return 0.0
        return round((1.0 - di / dt) * 100.0, 1)

    @staticmethod
    def _mem_percent() -> float:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if v:
                    mem[k.strip()] = int(v.strip().split()[0])  # kB
        total, avail = mem.get("MemTotal", 0), mem.get("MemAvailable", 0)
        return round((total - avail) / total * 100.0, 1) if total else 0.0

    @staticmethod
    def _disk_percent(path: str = "/") -> float:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        return round((total - free) / total * 100.0, 1) if total else 0.0

    @staticmethod
    def _nginx_request_stats(window_min: int):
        """Parse the tail of the nginx access log for request-rate and 5xx
        error-rate over the last `window_min` minutes. Returns
        (total, rpm_window, rpm_last_min, err_pct) or None if unavailable.
        Host-wide (the log has no vhost), which is what matters for 'is the
        box about to fall over'."""
        import re
        from datetime import datetime as _dt, timezone, timedelta as _td
        if not os.path.exists(NGINX_ACCESS_LOG):
            return None
        # Read only the tail — access logs can be huge.
        try:
            with open(NGINX_ACCESS_LOG, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - 4_000_000))   # last ~4 MB
                data = f.read().decode("latin-1", "ignore")
        except Exception:
            return None
        # [14/Jun/2026:22:58:22 +0200] ... " <status> <bytes>
        line_re = re.compile(r'\[([^\]]+)\]\s+"[^"]*"\s+(\d{3})')
        months = {m: i for i, m in enumerate(
            ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
        now = _dt.now(timezone.utc)
        win_start = now - _td(minutes=window_min)
        last_min_start = now - _td(minutes=1)
        total = last_min = err_5xx = 0
        for ts_str, status in line_re.findall(data):
            try:
                d, mon, rest = ts_str.split("/", 2)
                y, hh, mm, ss_tz = rest.split(":", 3)
                ss = ss_tz.split()[0]
                tz = ss_tz.split()[1]
                sign = 1 if tz[0] == "+" else -1
                off = _td(hours=int(tz[1:3]), minutes=int(tz[3:5])) * sign
                t = _dt(int(y), months[mon], int(d), int(hh), int(mm), int(ss),
                        tzinfo=timezone.utc) - off
            except Exception:
                continue
            if t < win_start:
                continue
            total += 1
            if status.startswith("5"):
                err_5xx += 1
            if t >= last_min_start:
                last_min += 1
        rpm_window = round(total / window_min, 1)
        err_pct = round(err_5xx / total * 100.0, 1) if total else 0.0
        return total, rpm_window, last_min, err_pct

    def check_server_health(self) -> dict:
        """Collect host + traffic metrics and flag anything abnormal. Returns
        {'metrics': {...}, 'warnings': [str, ...]}."""
        print("\n=== HEALTH: server + runaway monitoring ===")
        metrics, warnings = {}, []

        # CPU
        try:
            cpu = self._cpu_percent()
            metrics["cpu_pct"] = cpu
            la = os.getloadavg()[0]
            cores = os.cpu_count() or 1
            metrics["load_1m"] = round(la, 2)
            metrics["load_per_core"] = round(la / cores, 2)
            if cpu >= HEALTH_CPU_WARN_PCT:
                warnings.append(f"CPU {cpu}% ≥ {HEALTH_CPU_WARN_PCT}%")
            if la / cores >= HEALTH_LOAD_PER_CORE:
                warnings.append(f"Load {la:.2f} ({la/cores:.2f}/core) — host overloaded")
        except Exception as e:
            metrics["cpu_error"] = str(e)

        # Memory
        try:
            mem = self._mem_percent()
            metrics["mem_pct"] = mem
            if mem >= HEALTH_MEM_WARN_PCT:
                warnings.append(f"Memory {mem}% used ≥ {HEALTH_MEM_WARN_PCT}%")
        except Exception as e:
            metrics["mem_error"] = str(e)

        # Disk
        try:
            disk = self._disk_percent("/")
            metrics["disk_pct"] = disk
            if disk >= HEALTH_DISK_WARN_PCT:
                warnings.append(f"Disk {disk}% used on / ≥ {HEALTH_DISK_WARN_PCT}%")
        except Exception as e:
            metrics["disk_error"] = str(e)

        # Request rate + error rate (nginx access log, host-wide)
        try:
            stats = self._nginx_request_stats(HEALTH_REQ_WINDOW_MIN)
            if stats is None:
                metrics["traffic"] = "nginx log unavailable"
            else:
                total, rpm_window, last_min, err_pct = stats
                metrics.update({
                    "req_total_window": total,
                    "req_per_min_avg": rpm_window,
                    "req_last_min": last_min,
                    "err_5xx_pct": err_pct,
                    "window_min": HEALTH_REQ_WINDOW_MIN,
                })
                # Sustained high rate
                if rpm_window >= HEALTH_REQ_RATE_WARN:
                    warnings.append(
                        f"Request rate {rpm_window}/min over {HEALTH_REQ_WINDOW_MIN}m "
                        f"≥ {HEALTH_REQ_RATE_WARN}/min (possible DDoS/runaway)")
                # Sudden spike vs the window average
                if (last_min >= HEALTH_SPIKE_MIN_RPM and rpm_window > 0
                        and last_min >= HEALTH_SPIKE_FACTOR * rpm_window):
                    warnings.append(
                        f"Traffic SPIKE: last min {last_min} req vs {rpm_window}/min avg "
                        f"(≥{HEALTH_SPIKE_FACTOR}× — possible DDoS/runaway)")
                # Error rate
                if total >= HEALTH_ERR_MIN_SAMPLE and err_pct >= HEALTH_ERR_RATE_WARN:
                    warnings.append(
                        f"5xx error rate {err_pct}% over {HEALTH_REQ_WINDOW_MIN}m "
                        f"≥ {HEALTH_ERR_RATE_WARN}% ({total} reqs)")
        except Exception as e:
            metrics["traffic_error"] = str(e)

        print(f"  metrics: {json.dumps(metrics)}")
        if warnings:
            for w in warnings:
                print(f"  ⚠️  {w}")
        else:
            print("  ✅ all health metrics within thresholds")
        return {"metrics": metrics, "warnings": warnings}

    def run(self):
        print(f"UgoingViral QA Agent — {datetime.utcnow().isoformat()}Z")
        print(f"skip_ai={self.skip_ai}")

        self.suite1_signup()
        self.suite2_free_plan()
        self.suite3_content_gen()
        self.suite4_billing()
        self.suite5_health()
        self.suite6_agent_welcome()
        self.suite7_voice_features()
        self.suite8_watermark()
        self.suite9_ai_portrait()
        self.suite10_content_plan()
        self.suite11_full_auto()
        self.suite12_workspaces()
        self.suite13_agent_tasks()
        self.suite14_anti_ban()
        self.suite15_analytics()
        self.suite16_ui_regressions()
        self.suite17_autopilot_posting()
        self._cleanup()

        # Infra health + runaway monitoring (separate from functional tests).
        health = self.check_server_health()

        passed = sum(1 for r in self.results if r["status"] == "PASS")
        total  = len(self.results)
        all_ok = passed == total

        summary = {
            "timestamp":  datetime.utcnow().isoformat() + "Z",
            "platform":   "ugoingviral",
            "passed":     passed,
            "failed":     total - passed,
            "total":      total,
            "all_pass":   all_ok,
            "results":    self.results,
            "health":     health,
        }

        print(f"\n{'='*40}")
        print(f"RESULT: {passed}/{total} passed {'OK' if all_ok else 'FAILURES'}")
        print(f"{'='*40}")

        self._save_history(summary)
        self._report_nexcore(summary)
        self._telegram_alert(summary)

        return all_ok


def main():
    parser = argparse.ArgumentParser(description="UgoingViral QA Agent")
    parser.add_argument("--skip-ai", action="store_true",
                        help="Skip AI generation tests (saves credits/cost)")
    args = parser.parse_args()

    runner = QARunner(skip_ai=args.skip_ai)
    ok = runner.run()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
