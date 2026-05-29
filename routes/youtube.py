from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from datetime import datetime, timedelta
from services.store import store, save_store, add_log
from routes.tiktok import _popup_close_html

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
    """Google OAuth callback — exchange code for tokens, save both access +
    refresh tokens to the user store, and close the popup."""
    if error or not code:
        add_log(f"❌ YouTube OAuth fejl: {error or 'ingen code'}", "error")
        return HTMLResponse(_popup_close_html("YouTube", "error", error or "no_code"))

    try:
        from youtube_api import exchange_code_for_token, get_channel_info
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

        now = datetime.utcnow().isoformat()

        # Google only returns refresh_token on the first consent. If the
        # new response omits it, fall back to whatever we already had.
        existing = store.get("youtube") or {}
        old_refresh = existing.get("refresh_token") or store.get("settings", {}).get("youtube_refresh_token", "")
        effective_refresh = refresh_token or old_refresh

        # Primary store: a single grouped youtube dict with all token data.
        store["youtube"] = {
            "connected":     True,
            "username":      channel_name,
            "channel_id":    channel_id,
            "access_token":  access_token,
            "refresh_token": effective_refresh,
            "token_expiry":  expires_at,
            "connected_at":  now,
            "last_sync":     now,
        }

        # Legacy flat keys in settings — kept in sync so existing consumers
        # (post endpoint, scheduler, etc.) keep working.
        s = store.setdefault("settings", {})
        s["youtube_api_connected"]  = True
        s["youtube_access_token"]   = access_token
        if effective_refresh:
            s["youtube_refresh_token"] = effective_refresh
        s["youtube_expires_at"]     = expires_at
        s["youtube_channel_id"]     = channel_id
        s["youtube_channel_name"]   = channel_name
        s["youtube_connected_at"]   = now
        s["youtube_last_sync"]      = now

        conns = store.setdefault("connections", {})
        conns["youtube"] = {"username": channel_name, "connected": True}
        store["connections"] = conns
        save_store()
        add_log(f"✅ YouTube API forbundet: {channel_name}", "success")
        return HTMLResponse(_popup_close_html("YouTube", "connected", channel_name))

    except Exception as e:
        add_log(f"❌ YouTube OAuth fejl: {e}", "error")
        return HTMLResponse(_popup_close_html("YouTube", "error", str(e)))


@router.get("/api/youtube/status")
async def youtube_status():
    """Return YouTube connection status. `connected` is true only when an
    access_token exists AND is not expired — auto-refreshing first via the
    stored refresh_token when needed."""
    yt = store.get("youtube") or {}
    s  = store.get("settings", {})

    access  = yt.get("access_token")  or s.get("youtube_access_token", "")
    refresh = yt.get("refresh_token") or s.get("youtube_refresh_token", "")
    expires = yt.get("token_expiry")  or s.get("youtube_expires_at", "")
    channel_name = yt.get("username") or s.get("youtube_channel_name", "")
    channel_id   = yt.get("channel_id") or s.get("youtube_channel_id", "")

    if not access:
        return {
            "connected":    False,
            "channel_name": channel_name,
            "channel_id":   channel_id,
            "token_expiry": expires,
        }

    def _is_expired(exp_iso: str) -> bool:
        if not exp_iso:
            return False
        try:
            return datetime.fromisoformat(exp_iso) <= datetime.utcnow()
        except Exception:
            return False

    expired = _is_expired(expires)
    if expired and refresh:
        try:
            from youtube_api import refresh_access_token
            data = await refresh_access_token(refresh)
            access  = data.get("access_token", access)
            # Google generally does not return a new refresh_token on refresh,
            # but use it if present.
            refresh = data.get("refresh_token") or refresh
            expires = data.get("access_token_expires_at", expires)
            now = datetime.utcnow().isoformat()
            yt_new = dict(yt)
            yt_new.update({
                "connected":     True,
                "access_token":  access,
                "refresh_token": refresh,
                "token_expiry":  expires,
                "last_sync":     now,
            })
            store["youtube"] = yt_new
            s["youtube_access_token"]  = access
            s["youtube_refresh_token"] = refresh
            s["youtube_expires_at"]    = expires
            s["youtube_last_sync"]     = now
            save_store()
            expired = _is_expired(expires)
        except Exception as e:
            add_log(f"⚠️ YouTube auto-refresh fejl: {e}", "error")

    connected = bool(access) and not expired
    return {
        "connected":    connected,
        "channel_name": channel_name,
        "channel_id":   channel_id,
        "token_expiry": expires,
        "expires_at":   expires,  # legacy field for older frontend code
    }


@router.post("/api/youtube/disconnect")
async def youtube_disconnect():
    for key in ("youtube_api_connected", "youtube_access_token", "youtube_refresh_token",
                "youtube_expires_at", "youtube_channel_id", "youtube_channel_name"):
        store.get("settings", {}).pop(key, None)
    try:
        del store["youtube"]
    except KeyError:
        pass
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
