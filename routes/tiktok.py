from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from services.store import store, save_store, add_log

router = APIRouter()


@router.get("/api/tiktok/connect")
async def tiktok_oauth_start():
    """Start TikTok OAuth flow"""
    try:
        from tiktok_api import build_authorization_url, TIKTOK_CLIENT_KEY
        if not TIKTOK_CLIENT_KEY:
            return {"status": "error", "message": "TIKTOK_CLIENT_KEY ikke konfigureret"}
        import base64, json as _json
        state = base64.urlsafe_b64encode(_json.dumps({"platform": "tiktok"}).encode()).decode()
        url = build_authorization_url(state)
        return RedirectResponse(url=url)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/tiktok/callback")
async def tiktok_oauth_callback(code: str = "", error: str = "", state: str = ""):
    """TikTok OAuth callback — exchange code for token"""
    if error or not code:
        return RedirectResponse(url="/app?tiktok=error")

    try:
        from tiktok_api import exchange_code_for_token, get_user_info
        from services.users import load_users
        from services.store import set_user_context
        users_data = load_users()
        users = users_data.get("users", [])
        active_user = next((u for u in users if u.get("is_active")), None)
        if active_user:
            set_user_context(active_user["id"])
        token_data = await exchange_code_for_token(code)
        access_token  = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        open_id       = token_data.get("open_id", "")
        expires_at    = token_data.get("access_token_expires_at", "")
        refresh_exp   = token_data.get("refresh_token_expires_at", "")

        # Hent bruger info
        user_info = {}
        try:
            user_info = await get_user_info(access_token)
        except Exception:
            pass

        username = user_info.get("display_name", "") or user_info.get("username", open_id)

        store.get("settings", {})["tiktok_api_connected"]    = True
        store.get("settings", {})["tiktok_access_token"]     = access_token
        store.get("settings", {})["tiktok_refresh_token"]    = refresh_token
        store.get("settings", {})["tiktok_open_id"]          = open_id
        store.get("settings", {})["tiktok_expires_at"]       = expires_at
        store.get("settings", {})["tiktok_refresh_expires"]  = refresh_exp
        store.get("settings", {})["tiktok_username"]         = username
        # Spejl til connections dict så UI viser det som forbundet
        conns = store.setdefault("connections", {})
        conns["tiktok"] = {"username": username, "connected": True}
        store["connections"] = conns
        save_store()
        add_log(f"✅ TikTok API forbundet: @{username}", "success")
        return RedirectResponse(url="/app?tiktok=connected")

    except Exception as e:
        add_log(f"❌ TikTok OAuth fejl: {e}", "error")
        return RedirectResponse(url="/app?tiktok=error")


@router.get("/api/tiktok/status")
async def tiktok_status():
    connected = store.get("settings", {}).get("tiktok_api_connected", False)
    username  = store.get("settings", {}).get("tiktok_username", "")
    expires   = store.get("settings", {}).get("tiktok_expires_at", "")
    return {"connected": connected, "username": username, "expires_at": expires}


@router.post("/api/tiktok/disconnect")
async def tiktok_disconnect():
    for key in ("tiktok_api_connected", "tiktok_access_token", "tiktok_refresh_token",
                "tiktok_open_id", "tiktok_expires_at", "tiktok_refresh_expires", "tiktok_username"):
        store.get("settings", {}).pop(key, None)
    conns = store.get("connections", {})
    conns.pop("tiktok", None)
    save_store()
    add_log("TikTok API forbindelse fjernet", "info")
    return {"status": "disconnected"}


@router.post("/api/tiktok/post")
async def tiktok_post(req: Request):
    """Direkte TikTok post endpoint"""
    d = await req.json()
    caption   = d.get("caption", "")
    video_url = d.get("video_url")
    image_url = d.get("image_url")
    privacy   = d.get("privacy_level", "SELF_ONLY")

    s = store.get("settings", {})
    token   = s.get("tiktok_access_token")
    refresh = s.get("tiktok_refresh_token", "")
    exp     = s.get("tiktok_expires_at")

    if not token:
        return {"status": "error", "message": "TikTok API ikke forbundet — gå til Connect"}

    try:
        from tiktok_api import refresh_token_if_needed, publish_to_tiktok
        new_token, new_refresh, new_exp = await refresh_token_if_needed(token, refresh, exp)
        if new_exp:
            s["tiktok_access_token"]    = new_token
            s["tiktok_refresh_token"]   = new_refresh or refresh
            s["tiktok_expires_at"]      = new_exp
            save_store()
            token = new_token

        result = await publish_to_tiktok(
            access_token=token,
            caption=caption,
            video_url=video_url,
            image_url=image_url,
            privacy_level=privacy,
        )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}
