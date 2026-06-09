from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from services.store import store, save_store, add_log
import os, httpx, base64, hashlib, secrets, urllib.parse
from datetime import datetime
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.env"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.env"))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), "../.env"))

router = APIRouter()

CLIENT_ID     = os.getenv("TWITTER_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("TWITTER_CLIENT_SECRET", "")
REDIRECT_URI  = "https://ugoingviral.com/api/twitter/callback"
SCOPES        = "tweet.read tweet.write users.read offline.access"

@router.get("/api/twitter/connect")
async def twitter_connect():
    code_verifier = secrets.token_urlsafe(32)
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b'=').decode()
    import json as _json
    _json.dump({"cv": code_verifier}, open("/tmp/twitter_code_verifier.json", "w"))
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": "ugoingviral",
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    url = "https://twitter.com/i/oauth2/authorize?" + urllib.parse.urlencode(params)
    return RedirectResponse(url=url)

@router.get("/api/twitter/callback")
async def twitter_callback(code: str = "", error: str = "", state: str = ""):
    if error or not code:
        return RedirectResponse(url="/app?twitter=error")
    try:
        from services.users import load_users
        from services.store import set_user_context
        users_data = load_users()
        users = users_data.get("users", [])
        active_user = next((u for u in users if u.get("is_active")), None)
        if active_user:
            set_user_context(active_user["id"])

            import json as _json
        cv_file = "/tmp/twitter_code_verifier.json"
        try:
            code_verifier = _json.load(open(cv_file)).get("cv", "")
        except:
            code_verifier = ""
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.twitter.com/2/oauth2/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": REDIRECT_URI,
                    "code_verifier": code_verifier,
                    "client_id": CLIENT_ID,
                },
                auth=(CLIENT_ID, CLIENT_SECRET),
            )
            token_data = r.json()

        print("TOKEN RESPONSE:", token_data)
        access_token = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")

        # Hent bruger info
        async with httpx.AsyncClient() as c:
            r = await c.get(
                "https://api.twitter.com/2/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_data = r.json().get("data", {})

        username = user_data.get("username", "")
        name = user_data.get("name", "Twitter bruger")

        store.get("settings", {})["twitter_api_connected"] = True
        store.get("settings", {})["twitter_access_token"] = access_token
        store.get("settings", {})["twitter_refresh_token"] = refresh_token
        store.get("settings", {})["twitter_username"] = username
        store.get("settings", {})["twitter_connected_at"] = datetime.utcnow().isoformat()
        store.get("settings", {})["twitter_last_sync"] = datetime.utcnow().isoformat()
        store.get("connections", {})["twitter"] = {"username": username, "connected": True}
        save_store()
        add_log(f"✅ Twitter/X API forbundet: @{username}", "success")
        return RedirectResponse(url="/app?twitter=connected")
    except Exception as e:
        add_log(f"❌ Twitter OAuth fejl: {e}", "error")
        return RedirectResponse(url="/app?twitter=error")

@router.get("/api/twitter/status")
async def twitter_status():
    s = store.get("settings", {})
    return {
        "connected": s.get("twitter_api_connected", False),
        "username": s.get("twitter_username", ""),
    }


async def _refresh_twitter_token(refresh_token: str) -> dict:
    """Exchange a refresh_token for a fresh access_token (OAuth2 offline.access)."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            "https://api.twitter.com/2/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
            },
            auth=(CLIENT_ID, CLIENT_SECRET),
        )
        return r.json()


async def post_tweet(text: str) -> dict:
    """Post a tweet for the current user via the X API v2.

    Auto-refreshes the access token on 401 using the stored refresh_token.
    Returns {status, tweet_id, url, message}. X caps tweets at 280 chars.
    """
    s = store.get("settings", {})
    token    = s.get("twitter_access_token", "")
    refresh  = s.get("twitter_refresh_token", "")
    username = s.get("twitter_username", "")
    if not token:
        return {"status": "error", "message": "X/Twitter token mangler — forbind igen"}

    body = {"text": (text or "")[:280]}

    def _build_url(tweet_id: str) -> str:
        handle = (username or "").lstrip("@").strip()
        if handle:
            return f"https://twitter.com/{handle}/status/{tweet_id}"
        return f"https://twitter.com/i/web/status/{tweet_id}"

    async with httpx.AsyncClient(timeout=30) as c:
        def _do(tok):
            return c.post(
                "https://api.twitter.com/2/tweets",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json=body,
            )
        r = await _do(token)
        # Token expired — refresh once and retry.
        if r.status_code == 401 and refresh:
            data = await _refresh_twitter_token(refresh)
            new_token = data.get("access_token")
            if new_token:
                token = new_token
                s["twitter_access_token"] = new_token
                if data.get("refresh_token"):
                    s["twitter_refresh_token"] = data["refresh_token"]
                s["twitter_last_sync"] = datetime.utcnow().isoformat()
                save_store()
                r = await _do(token)

        if r.status_code in (200, 201):
            tweet_id = (r.json().get("data", {}) or {}).get("id", "")
            return {"status": "published", "tweet_id": tweet_id, "url": _build_url(tweet_id)}

        # 402 Payment Required — the X API tier has no posting credits.
        # Surface a clear, non-technical message instead of a raw API error.
        if r.status_code == 402:
            return {
                "status": "error",
                "code": 402,
                "message": ("X/Twitter posting requires an API upgrade. "
                            "Contact support@ugoingviral.com to enable Twitter posting for your account."),
            }

        # Surface a useful error message from the API response.
        err = ""
        try:
            j = r.json()
            err = (j.get("detail") or j.get("title")
                   or (j.get("errors", [{}])[0].get("message") if j.get("errors") else "")
                   or str(j))
        except Exception:
            err = (r.text or "")[:200]
        return {"status": "error", "message": f"X/Twitter API {r.status_code}: {err}"}


@router.post("/api/twitter/post")
async def twitter_post(req: Request):
    """Direct tweet endpoint — posts the given text and returns the tweet URL."""
    try:
        data = await req.json()
    except Exception:
        data = {}
    text = data.get("text") or data.get("content") or ""
    if not text.strip():
        return {"status": "error", "message": "Tom besked"}
    if not store.get("settings", {}).get("twitter_api_connected"):
        return {"status": "error", "message": "X/Twitter API ikke forbundet — gå til Connect"}
    return await post_tweet(text)
