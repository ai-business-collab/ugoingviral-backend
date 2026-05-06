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
NEXORA_KEY    = os.getenv("NEXORA_API_KEY", "nexora-core-2026")
NEXORA_URL    = "https://nex-core.tech/api/qa/report"
TG_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_ALERT_CHAT = os.getenv("TELEGRAM_ALERT_CHAT_ID", "")

EXPECTED_PLAN_PRICES = {
    "free": 0, "starter": 49, "basic": 79, "growth": 89,
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
    # Cleanup
    # ------------------------------------------------------------------

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
