from fastapi import APIRouter
from fastapi.responses import RedirectResponse
from services.store import store, save_store, add_log
import os, httpx, base64, hashlib, secrets, urllib.parse
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

        store["settings"]["twitter_api_connected"] = True
        store["settings"]["twitter_access_token"] = access_token
        store["settings"]["twitter_refresh_token"] = refresh_token
        store["settings"]["twitter_username"] = username
        store["connections"]["twitter"] = {"username": username, "connected": True}
        save_store()
        add_log(f"✅ Twitter/X API forbundet: @{username}", "success")
        return RedirectResponse(url="/app?twitter=connected")
    except Exception as e:
        add_log(f"❌ Twitter OAuth fejl: {e}", "error")
        return RedirectResponse(url="/app?twitter=error")

@router.get("/api/twitter/status")
async def twitter_status():
    s = store["settings"]
    return {
        "connected": s.get("twitter_api_connected", False),
        "username": s.get("twitter_username", ""),
    }
