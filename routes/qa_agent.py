"""
UgoingViral QA Agent
POST /api/qa/run     — run full test suite (X-Nexora-Key required)
GET  /api/qa/results — last 10 test runs   (X-Nexora-Key required)
"""
import os, json, time
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request
import httpx

router = APIRouter()

NEXORA_KEY   = os.getenv("NEXORA_API_KEY", "nexora-core-2026")
BASE          = "http://localhost:8000"
RESULTS_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "qa_results.json")

QA_EMAIL    = "qa-agent@ugoingviral.com"
QA_PASSWORD = "QA_AutoTest_2026!"


# Minimal valid MP3 frame: MPEG1 Layer3 sync word + header + silent frame data
# The uploads endpoint accepts audio/mpeg without validating audio content.
_TEST_MP3 = b"\xff\xfb\x90\x00" + b"\x00" * 413  # sync + one silent frame


def _require_key(request: Request):
    if request.headers.get("X-Nexora-Key", "") != NEXORA_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Nexora-Key")


def _load_results() -> list:
    if os.path.exists(RESULTS_FILE):
        try:
            with open(RESULTS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_results(results: list):
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump(results[-50:], f, indent=2)


async def _run_tests() -> dict:
    tests = []
    user_token = None

    async with httpx.AsyncClient(base_url=BASE, timeout=15.0) as c:

        # ── 1. Health check ──────────────────────────────────────────
        tests.append(await _test(
            c, "health_check", "GET", "/health",
            expect_status=200, expect_keys=["status"],
        ))

        # ── 2. User signup ───────────────────────────────────────────
        t = await _test(
            c, "user_signup", "POST", "/api/auth/register",
            body={"email": QA_EMAIL, "password": QA_PASSWORD,
                  "name": "QA Agent", "niche": "qa-test"},
            expect_status=[200, 400],
        )
        # 400 "already registered" counts as pass
        if t["status_code"] == 400:
            detail = str(t.get("_body", {}).get("detail", "")).lower()
            if any(w in detail for w in ("already", "exists", "registered")):
                t["pass"] = True
                t["note"] = "user already exists"
        tests.append(t)

        # ── 3. User login ────────────────────────────────────────────
        t = await _test(
            c, "user_login", "POST", "/api/auth/login",
            body={"email": QA_EMAIL, "password": QA_PASSWORD},
            expect_status=200, expect_keys=["token"],
        )
        if t["pass"]:
            user_token = t.get("_body", {}).get("token")
        tests.append(t)

        # ── 4. Billing plan ──────────────────────────────────────────
        tests.append(await _test(
            c, "billing_plan", "GET", "/api/billing/plan",
            headers=_auth(user_token), expect_status=200,
        ))

        # ── 5. Admin login ───────────────────────────────────────────
        tests.append(await _test(
            c, "admin_login", "POST", "/api/admin/login",
            body={"email": "admin@ugoingviral.com",
                  "password": os.getenv("ADMIN_PASSWORD", "***REDACTED-ADMIN-PASSWORD***")},
            expect_status=200, expect_keys=["token"],
        ))

        # ── 6. Autopilot toggle ──────────────────────────────────────
        tests.append(await _test(
            c, "autopilot_toggle", "POST", "/api/autopilot/toggle",
            body={"active": False},
            headers=_auth(user_token), expect_status=200, expect_keys=["ok"],
        ))

        # ── 7. Content engine (calendar read — no AI call) ───────────
        tests.append(await _test(
            c, "content_engine", "GET", "/api/content-engine/calendar",
            headers=_auth(user_token), expect_status=[200, 404],
        ))

        # ── 8. Upload media (1×1 PNG) ────────────────────────────────
        tests.append(await _test_upload(c, "upload_media", user_token))

    passed = sum(1 for t in tests if t["pass"])
    failed = len(tests) - passed
    return {
        "run_id":    datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "passed":    passed,
        "failed":    failed,
        "total":     len(tests),
        "all_pass":  failed == 0,
        "tests":     [{k: v for k, v in t.items() if not k.startswith("_")}
                      for t in tests],
    }


# ── Helpers ───────────────────────────────────────────────────────────────

def _auth(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


async def _test(
    c: httpx.AsyncClient,
    name: str,
    method: str,
    path: str,
    *,
    body=None,
    headers=None,
    expect_status=200,
    expect_keys=None,
) -> dict:
    t0 = time.monotonic()
    status_code = 0
    resp_body = {}
    error = None
    try:
        kwargs: dict = {}
        if body is not None:
            kwargs["json"] = body
        if headers:
            kwargs["headers"] = headers
        resp = await getattr(c, method.lower())(path, **kwargs)
        status_code = resp.status_code
        try:
            resp_body = resp.json()
        except Exception:
            resp_body = {"_text": resp.text[:200]}
    except Exception as e:
        error = str(e)[:120]

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    allowed = expect_status if isinstance(expect_status, list) else [expect_status]
    passes = (
        error is None
        and status_code in allowed
        and all(k in resp_body for k in (expect_keys or []))
    )
    return {
        "name":        name,
        "pass":        passes,
        "status_code": status_code,
        "elapsed_ms":  elapsed_ms,
        "error":       error,
        "note":        "",
        "_body":       resp_body,
    }


async def _test_upload(c: httpx.AsyncClient, name: str, token: str | None) -> dict:
    t0 = time.monotonic()
    status_code = 0
    error = None
    try:
        resp = await c.post(
            "/api/uploads/media",
            files={"file": ("qa_test.mp3", _TEST_MP3, "audio/mpeg")},
            headers=_auth(token),
        )
        status_code = resp.status_code
    except Exception as e:
        error = str(e)[:120]

    elapsed_ms = round((time.monotonic() - t0) * 1000)
    return {
        "name":        name,
        "pass":        error is None and status_code in (200, 201),
        "status_code": status_code,
        "elapsed_ms":  elapsed_ms,
        "error":       error,
        "note":        "",
    }


# ── Endpoints ─────────────────────────────────────────────────────────────

@router.post("/api/qa/run")
async def qa_run(request: Request):
    _require_key(request)
    run = await _run_tests()
    results = _load_results()
    results.append(run)
    _save_results(results)
    return run


@router.get("/api/qa/results")
def qa_results(request: Request):
    _require_key(request)
    results = _load_results()
    return {"runs": results[-10:]}
