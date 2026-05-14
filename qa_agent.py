#!/opt/ugoingviral/bin/python3
"""UgoingViral QA Agent — Standalone cron script"""
import os, sys, json, time, argparse
from datetime import datetime

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
USERS_FILE    = os.path.join(BASE_DIR, "users.json")
NEXORA_KEY    = os.getenv("NEXORA_API_KEY", "nexora-core-2026")
NEXORA_URL    = "https://nex-core.tech/api/qa/report"
TG_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ALERT_CHAT = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")

EXPECTED_PLAN_PRICES = {
    "free": 0, "starter": 49, "basic": 79, "growth": 299,
    "pro": 149, "elite": 229, "personal": 459, "agency": 199,
}
EXPECTED_CREDIT_PACKAGES = {100: 8, 350: 24, 700: 44, 1500: 79}


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

    def _fresh_user(self, label: str):
        """Register a disposable test user. Returns (token, user_id, email).

        Retries on 429 (rate limit) with a long backoff so consecutive
        suite calls don't collide on the 5/min register limit.
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
                    return token, user_id, email
                if r.status_code != 429:
                    break
            except Exception:
                pass
        return None, None, email

    def _set_billing(self, user_id: str, plan: str, credits: int) -> bool:
        """Directly write billing plan + credits into the user's JSON store."""
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
        """Delete a disposable test user from users.json and remove their data file."""
        try:
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, encoding="utf-8") as f:
                    users = json.load(f)
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
        if not failed:
            return
        lines = [f"*UgoingViral QA FAILED* — {summary['timestamp']}",
                 f"{summary['passed']}/{summary['total']} passed", ""]
        for r in failed[:10]:
            lines.append(f"❌ `{r['name']}`: {r['detail']}")
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
    # Entry point
    # ------------------------------------------------------------------

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
        self._cleanup()

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
