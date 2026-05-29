from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, HTMLResponse
from datetime import datetime, timedelta
from services.store import store, save_store, add_log

router = APIRouter()


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

        s = store.setdefault("settings", {})
        s["tiktok_api_connected"]    = True
        s["tiktok_access_token"]     = access_token
        s["tiktok_refresh_token"]    = refresh_token
        s["tiktok_open_id"]          = open_id
        s["tiktok_expires_at"]       = expires_at
        s["tiktok_refresh_expires"]  = refresh_exp
        s["tiktok_username"]         = username
        s["tiktok_connected_at"]     = datetime.utcnow().isoformat()
        s["tiktok_last_sync"]        = datetime.utcnow().isoformat()
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
    s = store.get("settings", {})
    connected = s.get("tiktok_api_connected", False)
    username  = s.get("tiktok_username", "")
    expires   = s.get("tiktok_expires_at", "")
    access    = s.get("tiktok_access_token", "")
    refresh   = s.get("tiktok_refresh_token", "")

    # Proactively refresh if token is close to expiry — TikTok access tokens
    # last ~24h, so the UI would flip to "expired" daily without this.
    valid = bool(connected and access)
    if connected and refresh and expires:
        try:
            from tiktok_api import refresh_token_if_needed
            new_token, new_refresh, new_exp = await refresh_token_if_needed(access, refresh, expires)
            if new_exp:
                s["tiktok_access_token"]  = new_token
                s["tiktok_refresh_token"] = new_refresh or refresh
                s["tiktok_expires_at"]    = new_exp
                s["tiktok_last_sync"]     = datetime.utcnow().isoformat()
                save_store()
                expires = new_exp
                valid = True
        except Exception as e:
            add_log(f"⚠️ TikTok auto-refresh fejl: {e}", "error")
            try:
                if datetime.fromisoformat(expires) < datetime.utcnow():
                    valid = False
            except Exception:
                valid = False

    return {
        "connected": bool(connected),
        "valid": valid,
        "username": username,
        "expires_at": expires,
    }


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
