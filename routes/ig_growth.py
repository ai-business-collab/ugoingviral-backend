"""
Instagram Growth connect — orchestrates a remote, headed Playwright session
the user controls via noVNC in their browser. The Playwright Chromium runs
on the VPS at DISPLAY=:99 (Xvfb), x11vnc bridges :99 to localhost:5900,
websockify exposes localhost:6080 as a WebSocket, and nginx proxies the
public `/vnc-ws/<token>/` path back to websockify. The user's iframe loads
`/vnc-static/vnc.html` and connects through that path.

Token is 32 base64url chars (~190 bits of entropy). Path-based auth: anyone
who knows the token URL during the 10-min session window can interact with
the open browser; after teardown, the browser is gone and any leaked URL
is moot. Single active session at a time (only one X display), enforced
via `_global_active_session()`.

The watcher polls `ctx.cookies()` every 2s; once `sessionid` is populated
and the page has navigated off `/accounts/login/`, we consider login
complete, persist cookies via `ctx.storage_state(path=COOKIE_FILE)`, mark
the session done, and tear the browser down.
"""
import asyncio
import gc
import logging
import os
import re
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from routes.auth import get_current_user
from services.store import _load_user_store, _save_user_store

logger = logging.getLogger(__name__)
router  = APIRouter()

GROWTH_PLUS = {"growth", "pro", "elite", "personal", "agency"}
SESSION_TIMEOUT_S = 10 * 60
SESSIONS_DIR = "/root/ugoingviral-backend/sessions"
COOKIE_FILE  = os.path.join(SESSIONS_DIR, "instagram_session.json")
PROFILE_DIR  = os.path.join(SESSIONS_DIR, "instagram_chrome_profile")
DISPLAY      = ":99"

# Active sessions: session_id -> {user_id, token, status, error, created_at, ...}
_SESSIONS: dict[str, dict] = {}

# Statuses considered "running" for the single-active-session lock.
_LIVE_STATUSES = {"starting", "waiting_login"}


def _is_growth_plus(user_id: str) -> bool:
    plan = (_load_user_store(user_id).get("billing", {}).get("plan") or "").lower()
    return plan in GROWTH_PLUS


def _global_active_session() -> Optional[str]:
    for sid, s in _SESSIONS.items():
        if s.get("status") in _LIVE_STATUSES:
            return sid
    return None


def _public_vnc_url(token: str) -> str:
    """noVNC reads `path=...` from the URL hash to build the WS URL.
    `&encrypt=1` forces wss when the page is loaded over https."""
    return (
        f"/vnc-static/vnc.html"
        f"#path=vnc-ws/{token}/"
        f"&autoconnect=true&resize=remote&encrypt=1&reconnect=false"
        f"&show_dot=true&view_only=false"
    )


async def _watch_and_run(session_id: str) -> None:
    """Background task: launch Playwright, navigate to IG login, watch for
    successful login, save cookies, tear down."""
    sess = _SESSIONS[session_id]
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(PROFILE_DIR,  exist_ok=True)
    # Ensure DISPLAY is exported for the Chromium child process.
    os.environ["DISPLAY"] = DISPLAY

    # Per-user sticky session so the same operator hits Instagram from a
    # stable exit IP every time they re-open the connect modal. 24h sticky.
    user_id    = sess.get("user_id") or ""
    sess_tag   = (user_id[:8] or "anon").lower()
    proxy_user = f"customer-ugoingviral_1reRN-sessid-{sess_tag}-sesstime-1440"
    proxy = {
        "server":   "dk-pr.oxylabs.io:19000",
        "username": proxy_user,
        "password": os.getenv("OXYLABS_PASSWORD", "Ugoingviral2026:"),
    }
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sess["status"] = "error"
        sess["error"]  = "Playwright not installed in the Python environment"
        return

    browser = ctx = None
    try:
        async with async_playwright() as p:
            ctx = await p.chromium.launch_persistent_context(
                PROFILE_DIR,
                headless=False,
                env={"DISPLAY": ":99"},
                proxy=proxy,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                ignore_https_errors=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            sess["status"] = "waiting_login"
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            try:
                await page.goto("https://www.instagram.com/accounts/login/", timeout=45_000)
            except Exception as exc:
                logger.warning("ig_growth: initial nav failed: %s", exc)

            deadline = asyncio.get_event_loop().time() + SESSION_TIMEOUT_S
            while asyncio.get_event_loop().time() < deadline:
                if sess.get("status") == "cancelled":
                    break
                try:
                    cookies = await ctx.cookies()
                    has_session = any(
                        c.get("name") == "sessionid" and c.get("value") for c in cookies
                    )
                    url = page.url if not page.is_closed() else ""
                    if has_session and "/accounts/login" not in url and "/accounts/onetap" not in url:
                        # Persist Playwright storage_state (cookies + localStorage)
                        await ctx.storage_state(path=COOKIE_FILE)
                        sess["status"] = "logged_in"
                        sess["cookies_saved_at"] = datetime.utcnow().isoformat()
                        break
                except Exception as exc:
                    logger.warning("ig_growth: poll error: %s", exc)
                await asyncio.sleep(2)
            else:
                sess["status"] = "timeout"

            # Brief grace so the user sees the success state in the iframe.
            try:
                await asyncio.sleep(2)
            except Exception:
                pass
    except Exception as exc:
        logger.exception("ig_growth: watcher crashed")
        sess["status"] = "error"
        sess["error"]  = str(exc)[:200]
    finally:
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass


@router.post("/api/automation/playwright/instagram_connect")
async def instagram_connect(request: Request, current_user: dict = Depends(get_current_user)):
    if not _is_growth_plus(current_user["id"]):
        raise HTTPException(status_code=403, detail="Instagram Growth connect requires the Growth plan or higher")

    busy = _global_active_session()
    if busy and _SESSIONS[busy].get("user_id") != current_user["id"]:
        raise HTTPException(status_code=429, detail="Another connect session is currently active. Try again in a few minutes.")
    # If THIS user has a stale session, mark it cancelled so we can start a new one.
    for sid, s in list(_SESSIONS.items()):
        if s.get("user_id") == current_user["id"] and s.get("status") in _LIVE_STATUSES:
            s["status"] = "cancelled"

    session_id = secrets.token_urlsafe(16)
    token      = secrets.token_urlsafe(24)
    _SESSIONS[session_id] = {
        "user_id":    current_user["id"],
        "token":      token,
        "status":     "starting",
        "created_at": datetime.utcnow().isoformat(),
    }
    asyncio.create_task(_watch_and_run(session_id))

    return {
        "session_id":      session_id,
        "vnc_url":         _public_vnc_url(token),
        "status":          "starting",
        "session_timeout": SESSION_TIMEOUT_S,
        "instructions":    "A browser will open Instagram inside the iframe. Log in with your account — we'll save the session automatically.",
    }


@router.get("/api/automation/playwright/session_status/{session_id}")
async def session_status(session_id: str, current_user: dict = Depends(get_current_user)):
    sess = _SESSIONS.get(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="Unknown session")
    if sess.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your session")
    return {
        "session_id":      session_id,
        "status":          sess.get("status"),
        "error":           sess.get("error"),
        "cookies_saved":   bool(sess.get("cookies_saved_at")),
        "cookies_saved_at": sess.get("cookies_saved_at", ""),
    }


@router.post("/api/automation/playwright/cancel_session/{session_id}")
async def cancel_session(session_id: str, current_user: dict = Depends(get_current_user)):
    sess = _SESSIONS.get(session_id)
    if sess and sess.get("user_id") == current_user["id"]:
        sess["status"] = "cancelled"
    return {"ok": True}


# ── Batch Instagram login via CSV ─────────────────────────────────────────────
# Charge 5 credits per *successful* connect. Failed accounts (wrong password,
# 2FA challenge, suspicious-login wall) cost nothing. Single batch in flight
# per user; max 50 accounts per batch (rate-limit hygiene + UX cap).

BATCH_CREDIT_COST   = 5
BATCH_MAX_ACCOUNTS  = 50
BATCH_PER_ACCOUNT_S = 60  # hard timeout per account
_BATCHES: dict[str, dict] = {}


def _safe_filename(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:80]


@router.post("/api/automation/playwright/batch_connect")
async def batch_connect(body: dict, current_user: dict = Depends(get_current_user)):
    if not _is_growth_plus(current_user["id"]):
        raise HTTPException(status_code=403, detail="Batch connect requires the Growth plan or higher")

    raw = body.get("accounts") or []
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="No accounts provided")
    if len(raw) > BATCH_MAX_ACCOUNTS:
        raise HTTPException(status_code=400, detail=f"Max {BATCH_MAX_ACCOUNTS} accounts per batch")

    accounts = []
    for a in raw:
        if not isinstance(a, dict):
            raise HTTPException(status_code=400, detail="Bad account format")
        u = (a.get("username") or "").strip().lstrip("@")
        p = (a.get("password") or "")
        if not u or not p:
            raise HTTPException(status_code=400, detail="Each account needs both username and password")
        accounts.append({"username": u, "password": p})

    # Single batch in flight per user.
    for bid, b in list(_BATCHES.items()):
        if b.get("user_id") == current_user["id"] and b.get("status") == "running":
            raise HTTPException(status_code=409, detail="A batch is already running")

    # Upfront credit pre-flight check (we charge per success, but if the user
    # can't afford the WHOLE batch we refuse to start so they don't get half-done).
    ustore  = _load_user_store(current_user["id"])
    billing = ustore.setdefault("billing", {})
    needed  = BATCH_CREDIT_COST * len(accounts)
    have    = int(billing.get("credits") or 0)
    if have < needed:
        raise HTTPException(
            status_code=402,
            detail=f"Need {needed} credits ({BATCH_CREDIT_COST} × {len(accounts)}), you have {have}",
        )

    batch_id = secrets.token_urlsafe(12)
    _BATCHES[batch_id] = {
        "user_id":          current_user["id"],
        "status":           "running",
        "total":            len(accounts),
        "completed":        0,
        "current_index":    0,
        "current_username": "",
        "results":          [],
        "started_at":       datetime.utcnow().isoformat(),
    }
    asyncio.create_task(_run_batch(batch_id, accounts))
    return {
        "batch_id": batch_id,
        "total":    len(accounts),
        "status":   "running",
        "credits_per_account": BATCH_CREDIT_COST,
        "credits_reserved":    needed,
    }


async def _human_pause(min_s: float, max_s: float) -> None:
    """Async-friendly randomized pause; replaces blocking time.sleep so the
    event loop keeps spinning while Playwright waits."""
    import random
    await asyncio.sleep(random.uniform(min_s, max_s))


async def _human_type(page, selector: str, text: str, focus_first: bool = True) -> None:
    """Type one character at a time with 50–150ms jitter, just like a human
    using the keyboard. Falls back to page.fill if focus fails."""
    import random
    if focus_first:
        try:
            await page.click(selector, timeout=10_000, delay=random.randint(20, 80))
        except Exception:
            pass
    for ch in text:
        try:
            await page.keyboard.type(ch, delay=int(random.uniform(50, 150)))
        except Exception:
            await page.fill(selector, text, timeout=10_000)
            return


async def _human_post_login_scroll(page) -> None:
    """A couple of small, randomized scrolls + idle pauses after login —
    mirrors the natural 'land on feed, look around' behavior."""
    import random
    try:
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(120, 480))
            await asyncio.sleep(random.uniform(0.6, 1.4))
        # Occasional upward scroll
        if random.random() < 0.4:
            await page.mouse.wheel(0, -random.randint(80, 200))
            await asyncio.sleep(random.uniform(0.4, 1.0))
    except Exception:
        pass


async def _login_one(p, account: dict, sessions_dir: str) -> dict:
    """Run a single Instagram headless login behind a per-account sticky DK
    proxy session and persist cookies on success. Each account gets its own
    sessid (derived from the username) so each login emerges from a distinct
    Oxylabs residential IP — Instagram never sees two of our accounts come
    from the same exit. Mutates `account` to wipe the password ASAP."""
    username    = account["username"]
    safe_user   = _safe_filename(username)
    # Per-account sticky session — `sesstime-1440` keeps the same exit IP
    # for 24h so a re-run of the same handle reuses its assigned IP.
    proxy_user  = f"customer-ugoingviral_1reRN-sessid-{safe_user}-sesstime-1440"
    proxy = {
        "server":   "dk-pr.oxylabs.io:19000",
        "username": proxy_user,
        "password": os.getenv("OXYLABS_PASSWORD", "Ugoingviral2026:"),
    }
    result   = {"username": username, "status": "pending", "proxy_session": safe_user}
    browser  = ctx = None
    try:
        browser = await p.chromium.launch(
            headless=True,
            proxy=proxy,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            ignore_https_errors=True,
        )
        page = await ctx.new_page()
        await page.goto("https://www.instagram.com/accounts/login/", timeout=45_000)

        # Cookie banner / GDPR overlay — best-effort dismiss.
        for sel in ('button:has-text("Allow all cookies")', 'button:has-text("Only allow essential cookies")'):
            try:
                await page.click(sel, timeout=2_500)
                break
            except Exception:
                pass

        # ── Human behavior simulation ─────────────────────────────────────────
        # Pause "looking at the form", then type the username one char at a
        # time, pause, type the password, pause, click submit.
        await _human_pause(1.5, 3.0)
        await _human_type(page, 'input[name="username"]', username)

        await _human_pause(0.8, 1.5)
        # Password kept in scope only across the typing call, then wiped.
        await _human_type(page, 'input[name="password"]', account["password"], focus_first=True)
        account["password"] = ""  # wipe the dict slot

        await _human_pause(0.5, 1.2)
        try:
            await page.click('button[type="submit"]', timeout=10_000)
        except Exception:
            await page.keyboard.press("Enter")

        # Watch for success or known dead-ends.
        deadline = asyncio.get_event_loop().time() + BATCH_PER_ACCOUNT_S
        while asyncio.get_event_loop().time() < deadline:
            url = page.url
            if "/challenge/" in url or "/two_factor/" in url:
                result["status"] = "needs_verification"
                result["error"]  = "Instagram requires extra verification (2FA / suspicious login)"
                break
            cookies = await ctx.cookies()
            if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
                if "/accounts/login" not in url and "/accounts/onetap" not in url:
                    # A real human would land on the feed and scroll a bit
                    # before "leaving" the tab. Doing so before saving the
                    # storage state means the cookie set is more representative
                    # of an active session.
                    await _human_post_login_scroll(page)
                    cookie_path = os.path.join(sessions_dir, f"instagram_{safe_user}_session.json")
                    await ctx.storage_state(path=cookie_path)
                    result["status"]      = "connected"
                    result["cookie_file"] = cookie_path
                    break
            await asyncio.sleep(1.5)
        else:
            result["status"] = "failed"
            result["error"]  = "Login did not complete in time (wrong credentials or rate-limited)"

        # Belt-and-braces: handle the case where the loop broke without a status set.
        if result["status"] == "pending":
            result["status"] = "failed"
            result["error"]  = "Unknown — no success indicator"
    except Exception as exc:
        logger.exception("batch_connect login error for %s", username)
        result["status"] = "error"
        result["error"]  = str(exc)[:200]
    finally:
        try:
            if ctx is not None:     await ctx.close()
        except Exception: pass
        try:
            if browser is not None: await browser.close()
        except Exception: pass
    return result


async def _run_batch(batch_id: str, accounts: list) -> None:
    state = _BATCHES.get(batch_id)
    if not state:
        return
    user_id = state["user_id"]
    os.makedirs(SESSIONS_DIR, exist_ok=True)

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        state["status"] = "error"
        state["error"]  = "Playwright not installed in the Python environment"
        return

    try:
        async with async_playwright() as p:
            for i, acct in enumerate(accounts):
                state["current_index"]    = i + 1
                state["current_username"] = acct["username"]

                result = await _login_one(p, acct, SESSIONS_DIR)
                # Drop the password reference even if _login_one already cleared it.
                acct["password"] = ""

                # Charge per success.
                if result.get("status") == "connected":
                    try:
                        ustore = _load_user_store(user_id)
                        bil    = ustore.setdefault("billing", {})
                        bil["credits"] = max(0, int(bil.get("credits") or 0) - BATCH_CREDIT_COST)
                        ustore["billing"] = bil
                        # Track connected IG handles per user so the UI can list them.
                        connected = ustore.setdefault("connected_instagram_growth", [])
                        if acct["username"] not in connected:
                            connected.append(acct["username"])
                        ustore["connected_instagram_growth"] = connected
                        _save_user_store(user_id, ustore)
                    except Exception:
                        logger.exception("batch_connect: credit deduction failed")

                state["results"].append(result)
                state["completed"] = i + 1
                # Force GC so the just-cleared password references actually get reclaimed.
                gc.collect()

        state["status"] = "done"
        state["finished_at"] = datetime.utcnow().isoformat()
    except Exception as exc:
        logger.exception("batch_connect: top-level crash")
        state["status"] = "error"
        state["error"]  = str(exc)[:200]
    finally:
        # Final defensive wipe — the input list may still be referenced by GC roots.
        for a in accounts:
            a["password"] = ""
        gc.collect()


@router.get("/api/automation/playwright/batch_status/{batch_id}")
async def batch_status(batch_id: str, current_user: dict = Depends(get_current_user)):
    state = _BATCHES.get(batch_id)
    if not state:
        raise HTTPException(status_code=404, detail="Unknown batch")
    if state.get("user_id") != current_user["id"]:
        raise HTTPException(status_code=403, detail="Not your batch")
    return {
        "batch_id":         batch_id,
        "status":           state.get("status"),
        "total":            state.get("total"),
        "completed":        state.get("completed"),
        "current_index":    state.get("current_index"),
        "current_username": state.get("current_username"),
        "results":          state.get("results", []),
        "error":            state.get("error"),
        "started_at":       state.get("started_at"),
        "finished_at":      state.get("finished_at"),
    }
