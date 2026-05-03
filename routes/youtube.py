from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from services.store import store, save_store, add_log

router = APIRouter()


@router.get("/api/youtube/connect")
async def youtube_oauth_start():
    """Start YouTube OAuth2 flow — redirect til Google login."""
    try:
        from youtube_api import build_authorization_url, YOUTUBE_CLIENT_ID
        if not YOUTUBE_CLIENT_ID:
            return {"status": "error", "message": "YOUTUBE_CLIENT_ID ikke konfigureret i .env"}
        import base64, json as _json
        state = base64.urlsafe_b64encode(_json.dumps({"platform": "youtube"}).encode()).decode()
        url = build_authorization_url(state)
        return RedirectResponse(url=url)
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/youtube/callback")
async def youtube_oauth_callback(code: str = "", error: str = "", state: str = ""):
    """Google OAuth callback — exchange code for tokens og gem kanal info."""
    if error or not code:
        add_log(f"❌ YouTube OAuth fejl: {error or 'ingen code'}", "error")
        return RedirectResponse(url="/app?youtube=error")

    try:
        from youtube_api import exchange_code_for_token, get_channel_info
        from services.users import load_users
        from services.store import set_user_context, reset_user_context, _save_user_store
        # Find første aktive bruger som fallback
        users_data = load_users()
        users = users_data.get("users", [])
        active_user = next((u for u in users if u.get("is_active")), None)
        if active_user:
            tokens = set_user_context(active_user["id"])
        token_data = await exchange_code_for_token(code)
        access_token  = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        expires_at    = token_data.get("access_token_expires_at", "")

        channel      = {}
        channel_name = "YouTube kanal"
        channel_id   = ""
        try:
            channel      = await get_channel_info(access_token)
            channel_id   = channel.get("id", "")
            channel_name = channel.get("snippet", {}).get("title", "YouTube kanal")
        except Exception:
            pass

        store.get("settings", {})["youtube_api_connected"]  = True
        store.get("settings", {})["youtube_access_token"]   = access_token
        store.get("settings", {})["youtube_refresh_token"]  = refresh_token
        store.get("settings", {})["youtube_expires_at"]     = expires_at
        store.get("settings", {})["youtube_channel_id"]     = channel_id
        store.get("settings", {})["youtube_channel_name"]   = channel_name
        conns = store.setdefault("connections", {})
        conns["youtube"] = {"username": channel_name, "connected": True}
        store["connections"] = conns
        save_store()
        add_log(f"✅ YouTube API forbundet: {channel_name}", "success")
        return RedirectResponse(url="/app?youtube=connected")

    except Exception as e:
        add_log(f"❌ YouTube OAuth fejl: {e}", "error")
        return RedirectResponse(url="/app?youtube=error")


@router.get("/api/youtube/status")
async def youtube_status():
    s = store.get("settings", {})
    return {
        "connected":    s.get("youtube_api_connected", False),
        "channel_name": s.get("youtube_channel_name", ""),
        "channel_id":   s.get("youtube_channel_id", ""),
        "expires_at":   s.get("youtube_expires_at", ""),
    }


@router.post("/api/youtube/disconnect")
async def youtube_disconnect():
    for key in ("youtube_api_connected", "youtube_access_token", "youtube_refresh_token",
                "youtube_expires_at", "youtube_channel_id", "youtube_channel_name"):
        store.get("settings", {}).pop(key, None)
    store.get("connections", {}).pop("youtube", None)
    save_store()
    add_log("YouTube API forbindelse fjernet", "info")
    return {"status": "disconnected"}


@router.post("/api/youtube/post")
async def youtube_post(req: Request):
    """Direkte YouTube post endpoint — upload video fra URL."""
    d = await req.json()
    title       = d.get("title", d.get("caption", "Nyt opslag"))
    description = d.get("description", d.get("content", ""))
    video_url   = d.get("video_url")
    is_short    = bool(d.get("is_short", False))
    privacy     = d.get("privacy_status", "public")
    tags        = d.get("tags", [])

    s = store.get("settings", {})
    token   = s.get("youtube_access_token")
    refresh = s.get("youtube_refresh_token", "")
    exp     = s.get("youtube_expires_at")

    if not token:
        return {"status": "error", "message": "YouTube API ikke forbundet — gå til Connect"}
    if not video_url:
        return {"status": "error", "message": "video_url er påkrævet"}

    try:
        from youtube_api import refresh_token_if_needed, publish_to_youtube
        new_token, new_exp = await refresh_token_if_needed(token, refresh, exp)
        if new_exp:
            s["youtube_access_token"] = new_token
            s["youtube_expires_at"]   = new_exp
            save_store()
            token = new_token

        result = await publish_to_youtube(
            access_token=token,
            title=title,
            description=description,
            video_url=video_url,
            tags=tags,
            privacy_status=privacy,
            is_short=is_short,
        )
        add_log(
            f"▶️ YouTube opslag: {result['status']} — {result.get('video_id', '')}",
            "success" if result["status"] == "published" else "error",
        )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}
