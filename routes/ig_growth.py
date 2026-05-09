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
import logging
import os
import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from routes.auth import get_current_user
from services.store import _load_user_store

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

    proxy = {
        "server":   "dk-pr.oxylabs.io:19000",
        "username": "customer-ugoingviral_1reRN",
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
