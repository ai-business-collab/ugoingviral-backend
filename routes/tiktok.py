from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from datetime import datetime, timedelta
import logging
import json as _json
from services.store import store, save_store, add_log
# Imported at module load (not lazily) so tiktok_api's logger — which logs the
# exact payloads sent to TikTok — is configured at startup, ensuring the request
# body is logged even on the very first post.
import tiktok_api

router = APIRouter()
logger = logging.getLogger("tiktok_api")


def _popup_close_html(platform: str, status: str, message: str = "") -> str:
    """Tiny HTML returned at the end of an OAuth popup flow.

    Notifies the opener window via postMessage and closes the popup. If the
    flow was started in the main tab (no opener), falls back to redirecting
    the tab to /app so the user is not stranded on a blank page.
    """
    safe_msg = (message or "").replace("\\", "\\\\").replace("`", "\\`")
    color = "#10b981" if status == "connected" else "#ef4444"
    label = "✅ Connected" if status == "connected" else "❌ Connection failed"
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{platform} OAuth</title>
<style>body{{font-family:system-ui;background:#0b0c10;color:#e5e7eb;display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}.box{{padding:32px;border:1px solid #1f2937;border-radius:12px}}h2{{color:{color};margin:0 0 8px}}p{{color:#9ca3af;font-size:13px;margin:6px 0}}</style></head>
<body><div class="box"><h2>{label}</h2><p>{platform}</p><p>{safe_msg}</p><p style="margin-top:16px;font-size:11px">This window closes automatically…</p></div>
<script>
(function(){{
  try {{
    if (window.opener && !window.opener.closed) {{
      window.opener.postMessage({{type:'oauth_result',platform:'{platform.lower()}',status:'{status}'}},'*');
      setTimeout(function(){{window.close();}}, 600);
    }} else {{
      setTimeout(function(){{location.replace('/app?{platform.lower()}={status}');}}, 800);
    }}
  }} catch(e) {{
    setTimeout(function(){{location.replace('/app?{platform.lower()}={status}');}}, 800);
  }}
}})();
</script></body></html>"""


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
    """TikTok OAuth callback — exchange code for token, persist, close popup."""
    if error or not code:
        return HTMLResponse(_popup_close_html("TikTok", "error", error or "no_code"))

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

        user_info = {}
        try:
            user_info = await get_user_info(access_token)
        except Exception:
            pass

        username = user_info.get("display_name", "") or user_info.get("username", open_id)

        now = datetime.utcnow().isoformat()

        # Primary store: a single grouped tiktok dict with all token data.
        store["tiktok"] = {
            "connected":            True,
            "username":             username,
            "open_id":              open_id,
            "access_token":         access_token,
            "refresh_token":        refresh_token,
            "token_expiry":         expires_at,
            "refresh_token_expiry": refresh_exp,
            "connected_at":         now,
            "last_sync":            now,
        }

        # Keep the legacy flat keys in settings up-to-date so older readers
        # (e.g. routes/tiktok.py:tiktok_post, analytics, scheduler) keep working.
        s = store.setdefault("settings", {})
        s["tiktok_api_connected"]    = True
        s["tiktok_access_token"]     = access_token
        s["tiktok_refresh_token"]    = refresh_token
        s["tiktok_open_id"]          = open_id
        s["tiktok_expires_at"]       = expires_at
        s["tiktok_refresh_expires"]  = refresh_exp
        s["tiktok_username"]         = username
        s["tiktok_connected_at"]     = now
        s["tiktok_last_sync"]        = now

        conns = store.setdefault("connections", {})
        conns["tiktok"] = {"username": username, "connected": True}
        store["connections"] = conns
        save_store()
        add_log(f"✅ TikTok API forbundet: @{username}", "success")
        return HTMLResponse(_popup_close_html("TikTok", "connected", f"@{username}"))

    except Exception as e:
        add_log(f"❌ TikTok OAuth fejl: {e}", "error")
        return HTMLResponse(_popup_close_html("TikTok", "error", str(e)))


@router.get("/api/tiktok/status")
async def tiktok_status():
    """Return TikTok connection status. `connected` is true only when an
    access_token exists AND is not expired — auto-refreshing first if a
    refresh_token is available."""
    tt = store.get("tiktok") or {}
    s  = store.get("settings", {})

    # Read from the grouped dict first, fall back to legacy flat keys so
    # accounts connected before the schema change keep working.
    access  = tt.get("access_token")  or s.get("tiktok_access_token", "")
    refresh = tt.get("refresh_token") or s.get("tiktok_refresh_token", "")
    expires = tt.get("token_expiry")  or s.get("tiktok_expires_at", "")
    username = tt.get("username") or s.get("tiktok_username", "")

    if not access:
        return {"connected": False, "username": username, "token_expiry": expires}

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
            from tiktok_api import refresh_access_token
            data = await refresh_access_token(refresh)
            access  = data.get("access_token", access)
            refresh = data.get("refresh_token", refresh)
            expires = data.get("access_token_expires_at", expires)
            now = datetime.utcnow().isoformat()
            tt_new = dict(tt)
            tt_new.update({
                "connected":     True,
                "access_token":  access,
                "refresh_token": refresh,
                "token_expiry":  expires,
                "last_sync":     now,
            })
            store["tiktok"] = tt_new
            s["tiktok_access_token"]  = access
            s["tiktok_refresh_token"] = refresh
            s["tiktok_expires_at"]    = expires
            s["tiktok_last_sync"]     = now
            save_store()
            expired = _is_expired(expires)
        except Exception as e:
            add_log(f"⚠️ TikTok auto-refresh fejl: {e}", "error")

    connected = bool(access) and not expired
    return {
        "connected":   connected,
        "username":    username,
        "open_id":     tt.get("open_id") or s.get("tiktok_open_id", ""),
        "token_expiry": expires,
        "expires_at":  expires,  # legacy field name for older frontend code
    }


async def _get_valid_tiktok_token() -> tuple[str, str]:
    """Return (access_token, username) for the current user, refreshing the
    token first if it is expired and a refresh_token is available. Returns
    ("", username) when no usable token exists."""
    tt = store.get("tiktok") or {}
    s  = store.get("settings", {})
    access  = tt.get("access_token")  or s.get("tiktok_access_token", "")
    refresh = tt.get("refresh_token") or s.get("tiktok_refresh_token", "")
    exp     = tt.get("token_expiry")  or s.get("tiktok_expires_at", "")
    username = tt.get("username") or s.get("tiktok_username", "")
    if not access:
        return "", username
    try:
        from tiktok_api import refresh_token_if_needed
        new_token, new_refresh, new_exp = await refresh_token_if_needed(access, refresh, exp)
        if new_exp:
            access = new_token
            s["tiktok_access_token"]  = new_token
            s["tiktok_refresh_token"] = new_refresh or refresh
            s["tiktok_expires_at"]    = new_exp
            if isinstance(tt, dict) and tt:
                tt["access_token"] = new_token
                tt["refresh_token"] = new_refresh or refresh
                tt["token_expiry"] = new_exp
                store["tiktok"] = tt
            save_store()
    except Exception as e:
        add_log(f"⚠️ TikTok token refresh fejl: {e}", "error")
    return access, username


@router.get("/api/tiktok/profile")
async def tiktok_profile():
    """Full TikTok profile for App-Review scope demonstration:
    user.info.basic (username, avatar), user.info.profile (bio, profile link),
    user.info.stats (follower/following/likes/video counts)."""
    token, username = await _get_valid_tiktok_token()
    if not token:
        return {"connected": False, "message": "TikTok ikke forbundet — gå til Connect"}
    try:
        from tiktok_api import get_user_info
        u = await get_user_info(token)
        # Persist the freshest username/handle for later post-URL building.
        if u.get("username"):
            store.get("settings", {})["tiktok_username"] = u.get("display_name") or username
        return {
            "connected":        True,
            # user.info.basic
            "open_id":          u.get("open_id", ""),
            "union_id":         u.get("union_id", ""),
            "display_name":     u.get("display_name", ""),
            "avatar_url":       u.get("avatar_url_100") or u.get("avatar_url") or u.get("avatar_large_url", ""),
            # user.info.profile
            "username":         u.get("username", ""),
            "bio_description":  u.get("bio_description", ""),
            "profile_deep_link": u.get("profile_deep_link", ""),
            "is_verified":      bool(u.get("is_verified", False)),
            # user.info.stats
            "follower_count":   u.get("follower_count", 0),
            "following_count":  u.get("following_count", 0),
            "likes_count":      u.get("likes_count", 0),
            "video_count":      u.get("video_count", 0),
        }
    except Exception as e:
        return {"connected": True, "error": str(e), "message": f"Kunne ikke hente TikTok profil: {e}"}


@router.get("/api/tiktok/videos")
async def tiktok_videos(cursor: int = 0, max_count: int = 20):
    """List the connected creator's own TikTok videos (video.list scope) for the
    Content Library "TikTok Videos" tab — thumbnail, title, views, likes."""
    token, _username = await _get_valid_tiktok_token()
    if not token:
        return {"connected": False, "videos": [], "message": "TikTok ikke forbundet — gå til Connect"}
    try:
        from tiktok_api import get_user_videos
        result = await get_user_videos(token, cursor=cursor, max_count=max_count)
        return {"connected": True, **result}
    except Exception as e:
        return {"connected": True, "videos": [], "error": str(e), "message": f"Kunne ikke hente TikTok videoer: {e}"}


@router.post("/api/tiktok/disconnect")
async def tiktok_disconnect():
    for key in ("tiktok_api_connected", "tiktok_access_token", "tiktok_refresh_token",
                "tiktok_open_id", "tiktok_expires_at", "tiktok_refresh_expires", "tiktok_username"):
        store.get("settings", {}).pop(key, None)
    try:
        del store["tiktok"]
    except KeyError:
        pass
    conns = store.get("connections", {})
    conns.pop("tiktok", None)
    save_store()
    add_log("TikTok API forbindelse fjernet", "info")
    return {"status": "disconnected"}


@router.post("/api/tiktok/post")
async def tiktok_post(req: Request):
    """Direkte TikTok post endpoint"""
    d = await req.json()
    logger.info("TikTok /post request body -> %s", _json.dumps(d, ensure_ascii=False))
    caption   = d.get("caption", "")
    video_url = d.get("video_url")
    image_url = d.get("image_url")
    # Explicit media type from the frontend (derived from the content-library
    # item's stored kind), e.g. "video" | "photo". Avoids guessing from the URL
    # extension, which breaks on extensionless/CDN URLs. Falls back to None so
    # publish_to_tiktok infers it from which URL is present.
    media_type = d.get("media_type") or d.get("kind")
    # Coerce to a TikTok-valid privacy level. A missing/empty/invalid value here
    # is what TikTok rejects with "The request post info is empty or incorrect".
    privacy   = tiktok_api.normalize_privacy_level(d.get("privacy_level"))

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

        username = (store.get("tiktok") or {}).get("username") or s.get("tiktok_username", "")
        result = await publish_to_tiktok(
            access_token=token,
            caption=caption,
            video_url=video_url,
            image_url=image_url,
            privacy_level=privacy,
            wait_for_url=True,   # interactive post — resolve the live TikTok URL
            username=username,
            media_type=media_type,
        )
        # Fall back to the creator's profile link when no public post URL is
        # available (e.g. SELF_ONLY posts on an unaudited app).
        if not result.get("post_url"):
            result["profile_url"] = (store.get("tiktok") or {}).get("profile_deep_link") \
                or (f"https://www.tiktok.com/@{username.lstrip('@')}" if username else "")
        add_log(
            f"🎵 TikTok post: {result.get('status')} — {result.get('post_url') or result.get('publish_id','')}",
            "success" if result.get("status") in ("published", "processing") else "error",
        )
        return result
    except Exception as e:
        return {"status": "error", "message": str(e)}
