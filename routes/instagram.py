from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log
from routes.auth import get_current_user
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ══════════════════════════════════════════════════════════
# Dual Instagram connection:
#   • Facebook Login  → post + comments + INSIGHTS (needs a linked FB Page)
#   • Instagram Login → post + comments (simple; no Page, NO insights)
# A per-user `instagram_connection_method` flag records which one is active so we
# never re-prompt, and _ig_target() routes posting to the right token/host.

def _decode_state_uid(state: str) -> str:
    """Recover the user_id we embedded in the OAuth state (binds the callback to
    the user who started the connect — the redirect carries no auth header)."""
    import base64, json as _json
    try:
        d = _json.loads(base64.urlsafe_b64decode(state.encode()).decode())
        return d.get("user_id", "") or ""
    except Exception:
        return ""


def _ig_target(s: dict) -> dict:
    """Pick which Instagram connection to use for posting. Prefers the method the
    user chose. Returns {method, base, ig_id, token, expires, token_key,
    expires_key} or {} when nothing is connected."""
    from instagram_api import GRAPH_BASE, IG_GRAPH_BASE
    method = s.get("instagram_connection_method", "")
    if method != "facebook" and s.get("instagram_login_token") and s.get("instagram_login_user_id"):
        return {"method": "instagram", "base": IG_GRAPH_BASE,
                "ig_id": s["instagram_login_user_id"], "token": s["instagram_login_token"],
                "expires": s.get("instagram_login_expires"),
                "token_key": "instagram_login_token", "expires_key": "instagram_login_expires"}
    if s.get("instagram_api_token") and s.get("instagram_ig_id"):
        return {"method": "facebook", "base": GRAPH_BASE,
                "ig_id": s["instagram_ig_id"], "token": s["instagram_api_token"],
                "expires": s.get("instagram_api_expires"),
                "token_key": "instagram_api_token", "expires_key": "instagram_api_expires"}
    return {}


@router.get("/api/instagram/oauth/start")
async def instagram_oauth_start():
    """Start the FACEBOOK-LOGIN flow (post + comments + insights via a linked Page)."""
    try:
        from instagram_api import build_authorization_url
        import base64, json as _json
        from services.store import _uid_ctx
        uid = _uid_ctx.get(None)
        payload = {"platform": "instagram", "method": "facebook"}
        if uid:
            payload["user_id"] = uid
        state = base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()
        return {"authorization_url": build_authorization_url(state), "state": state}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/instagram/login/start")
async def instagram_login_start():
    """Start the INSTAGRAM-LOGIN flow (simple: post + comments, no Facebook Page)."""
    try:
        from instagram_api import build_instagram_login_url
        import base64, json as _json
        from services.store import _uid_ctx
        uid = _uid_ctx.get(None)
        payload = {"platform": "instagram", "method": "instagram"}
        if uid:
            payload["user_id"] = uid
        state = base64.urlsafe_b64encode(_json.dumps(payload).encode()).decode()
        return {"authorization_url": build_instagram_login_url(state), "state": state}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@router.get("/api/instagram/login/callback")
async def instagram_login_callback(code: str = "", state: str = "", error: str = ""):
    """Instagram-Login OAuth callback → store the IG token per-user (method=instagram)."""
    if error:
        return {"status": "error", "message": error}
    if not code:
        return {"status": "error", "message": "Ingen code modtaget"}
    try:
        from instagram_api import ig_login_exchange_code, ig_login_long_lived, ig_login_account_info
        from services.store import _load_user_store, _save_user_store, _uid_ctx
        tok = await ig_login_exchange_code(code)
        short = tok.get("access_token")
        if not short:
            return {"status": "error", "message": "Ingen access token"}
        long_data = await ig_login_long_lived(short)
        long_token = long_data.get("access_token", short)
        info = await ig_login_account_info(long_token)
        ig_user_id = str(info.get("id", "") or tok.get("user_id", "") or "")
        uid = _decode_state_uid(state) or _uid_ctx.get(None)
        if not uid:
            return {"status": "error", "message": "Session udløbet — log ind og prøv igen."}
        us = _load_user_store(uid)
        s = us.setdefault("settings", {})
        s["instagram_login_token"]   = long_token
        s["instagram_login_expires"] = long_data.get("expires_at")
        s["instagram_login_user_id"] = ig_user_id
        s["instagram_username"]      = info.get("username", "")
        s["instagram_api_connected"] = True
        s["instagram_connection_method"] = "instagram"
        s["instagram_connected_at"]  = datetime.utcnow().isoformat()
        _save_user_store(uid, us)
        add_log(f"✅ Instagram (direct login) forbundet: @{info.get('username', ig_user_id)}", "success")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/app?instagram_connected=1")
    except Exception as e:
        add_log(f"❌ Instagram Login fejl: {str(e)[:100]}", "error")
        return {"status": "error", "message": str(e)}

@router.get("/api/instagram/callback")
async def instagram_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Facebook OAuth callback → connect a Facebook PAGE (independent of Instagram).
    Stores the page access token/name under facebook_* so it stands on its own."""
    if error:
        return {"status": "error", "message": error}
    if not code:
        return {"status": "error", "message": "Ingen code modtaget"}
    try:
        from instagram_api import exchange_code_for_token, get_long_lived_token
        import httpx as _httpx
        token_data = await exchange_code_for_token(code)
        short_token = token_data.get("access_token")
        if not short_token:
            return {"status": "error", "message": "Ingen access token"}
        long_data = await get_long_lived_token(short_token)
        long_token = long_data.get("access_token", short_token)
        async with _httpx.AsyncClient() as c:
            r = await c.get("https://graph.facebook.com/v19.0/me/accounts",
                            params={"access_token": long_token,
                                    "fields": "id,name,access_token,instagram_business_account"})
        pages = (r.json() or {}).get("data", [])
        if not pages:
            return {"status": "error", "message": "Ingen Facebook side fundet — opret en Facebook Page og prøv igen."}
        page = pages[0]
        from services.store import _load_user_store, _save_user_store, _uid_ctx
        uid = _decode_state_uid(state) or _uid_ctx.get(None)
        if not uid:
            return {"status": "error", "message": "Session udløbet — log ind og prøv igen."}
        us = _load_user_store(uid)
        s = us.setdefault("settings", {})
        s["facebook_page_token"]   = page.get("access_token", "")
        s["facebook_page_id"]      = page.get("id", "")
        s["facebook_page_name"]    = page.get("name", "")
        s["facebook_user_token"]   = long_token
        s["facebook_connected"]    = True
        s["facebook_connected_at"] = datetime.utcnow().isoformat()
        _save_user_store(uid, us)
        add_log(f"✅ Facebook side forbundet: {page.get('name', '')}", "success")
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/app?facebook_connected=1")
    except Exception as e:
        add_log(f"❌ Facebook OAuth fejl: {str(e)[:100]}", "error")
        return {"status": "error", "message": str(e)}


@router.get("/api/instagram/status")
async def instagram_api_status():
    """Instagram (Instagram Login) connection status — its own account, independent
    of Facebook. Insights ARE available on Instagram Login now."""
    s = store.get("settings", {})
    connected = bool(s.get("instagram_login_token") and s.get("instagram_login_user_id"))
    # Legacy: users who connected Instagram via the old Facebook/Page flow.
    legacy = (not connected) and bool(s.get("instagram_api_token") and s.get("instagram_ig_id"))
    return {
        "connected": connected or legacy,
        "method": "instagram" if connected else ("facebook" if legacy else ""),
        "username": s.get("instagram_username", ""),
        "expires_at": s.get("instagram_login_expires") if connected else s.get("instagram_api_expires", ""),
        "insights_available": connected or legacy,
        "mode": "api" if (connected or legacy) else "playwright",
    }


@router.get("/api/facebook/status")
async def facebook_status():
    """Facebook Page connection status — stands on its own beside Instagram."""
    s = store.get("settings", {})
    return {
        "connected": bool(s.get("facebook_page_token") and s.get("facebook_page_id")),
        "page_name": s.get("facebook_page_name", ""),
    }


@router.post("/api/instagram/disconnect")
async def instagram_disconnect(current_user: dict = Depends(get_current_user)):
    """Disconnect the Instagram connection (clears IG-Login + any legacy IG fields)."""
    from services.store import _load_user_store, _save_user_store
    uid = current_user["id"]
    us = _load_user_store(uid)
    s = us.setdefault("settings", {})
    for k in ["instagram_login_token", "instagram_login_user_id", "instagram_login_expires",
              "instagram_username", "instagram_connection_method",
              "instagram_api_token", "instagram_api_expires", "instagram_ig_id",
              "instagram_api_username", "instagram_api_connected", "instagram_connected_at",
              "instagram_last_sync", "instagram_user", "instagram_pass"]:
        s.pop(k, None)
    _save_user_store(uid, us)
    return {"ok": True}


@router.post("/api/facebook/disconnect")
async def facebook_disconnect(current_user: dict = Depends(get_current_user)):
    """Disconnect the Facebook Page connection."""
    from services.store import _load_user_store, _save_user_store
    uid = current_user["id"]
    us = _load_user_store(uid)
    s = us.setdefault("settings", {})
    for k in ["facebook_page_token", "facebook_page_id", "facebook_page_name",
              "facebook_user_token", "facebook_connected", "facebook_connected_at"]:
        s.pop(k, None)
    _save_user_store(uid, us)
    return {"ok": True}

@router.get("/api/instagram/posts")
async def instagram_posts(limit: int = 20):
    """List the connected Instagram account's own posts for the Content Library
    'Instagram Posts' tab — thumbnail, caption, likes/comments, date."""
    s = store.get("settings", {})
    token = s.get("instagram_api_token")
    ig_id = s.get("instagram_ig_id")
    if not token or not ig_id:
        return {"connected": False, "posts": [], "message": "Instagram ikke forbundet — gå til Connect"}
    try:
        from instagram_api import get_user_media, refresh_token_if_needed
        expires_at = s.get("instagram_api_expires")
        token, new_expires = await refresh_token_if_needed(token, expires_at)
        if new_expires:
            s["instagram_api_expires"] = new_expires
            s["instagram_api_token"]   = token
            save_store()
        posts = await get_user_media(ig_id, token, limit=limit)
        return {"connected": True, "posts": posts}
    except Exception as e:
        return {"connected": True, "posts": [], "error": str(e), "message": f"Kunne ikke hente Instagram opslag: {e}"}


@router.post("/api/instagram/post")
async def instagram_api_post(req: Request):
    """Post til Instagram via officiel API"""
    d = await req.json()
    caption = d.get("caption", "")
    image_url = d.get("image_url")
    image_urls = d.get("image_urls", [])
    video_url = d.get("video_url")
    media_type = d.get("media_type", "IMAGE")

    s = store.get("settings", {})
    tgt = _ig_target(s)
    if not tgt:
        return {"status": "error", "message": "Instagram ikke forbundet — gå til Connect"}

    try:
        from instagram_api import post_to_instagram, refresh_token_if_needed, ig_login_refresh_if_needed
        token = tgt["token"]
        # Refresh the right token type before posting.
        if tgt["method"] == "facebook":
            token, new_expires = await refresh_token_if_needed(token, tgt["expires"])
        else:
            token, new_expires = await ig_login_refresh_if_needed(token, tgt["expires"])
        if new_expires and new_expires != tgt["expires"]:
            s[tgt["token_key"]] = token
            s[tgt["expires_key"]] = new_expires
            save_store()

        result = await post_to_instagram(
            tgt["ig_id"], token, caption,
            image_url=image_url,
            image_urls=image_urls if image_urls else None,
            video_url=video_url,
            media_type=media_type,
            base=tgt["base"],          # graph.facebook.com or graph.instagram.com
        )
        if result.get("status") == "published":
            add_log(f"✅ Instagram API: opslag postet! ID: {result.get('id')}", "success")
        else:
            add_log(f"❌ Instagram API fejl: {result.get('message')}", "error")
        return result
    except Exception as e:
        add_log(f"❌ Instagram post fejl: {str(e)[:100]}", "error")
        return {"status": "error", "message": str(e)}

_INSIGHTS_NONE = {"status": "error", "message": "Instagram ikke forbundet — gå til Connect"}


@router.get("/api/instagram/insights/{post_id}")
async def instagram_post_insights(post_id: str):
    """Post metrics — Instagram Login (graph.instagram.com), or legacy IG-via-FB."""
    s = store.get("settings", {})
    if s.get("instagram_login_token"):
        from instagram_api import ig_login_media_insights
        return await ig_login_media_insights(post_id, s["instagram_login_token"])
    if s.get("instagram_api_token"):
        from instagram_api import get_post_insights
        return await get_post_insights(post_id, s["instagram_api_token"])
    return _INSIGHTS_NONE

@router.get("/api/instagram/account/insights")
async def instagram_account_insights():
    """Account metrics — Instagram Login (graph.instagram.com), or legacy IG-via-FB."""
    s = store.get("settings", {})
    if s.get("instagram_login_token") and s.get("instagram_login_user_id"):
        from instagram_api import ig_login_account_insights
        return await ig_login_account_insights(s["instagram_login_user_id"], s["instagram_login_token"])
    if s.get("instagram_api_token") and s.get("instagram_ig_id"):
        from instagram_api import get_account_insights
        return await get_account_insights(s["instagram_ig_id"], s["instagram_api_token"])
    return _INSIGHTS_NONE

# ══════════════════════════════════════════════════════════
# SCHEDULER — kører automatisk i bagg@router.post("/api/instagram/post/test")
async def instagram_post_test(current_user: dict = Depends(get_current_user)):
    """Test Instagram API connection — verifies token is valid"""
    ig_token = store.get("settings", {}).get("instagram_api_token")
    ig_id = store.get("settings", {}).get("instagram_ig_id")
    connected = store.get("settings", {}).get("instagram_api_connected", False)

    if not ig_token or not ig_id or not connected:
        return {
            "status": "error",
            "message": "Instagram not connected",
            "reconnect_required": True,
            "action": "Go to Connect page and reconnect Instagram"
        }

    try:
        from instagram_api import refresh_token_if_needed
        expires_at = store.get("settings", {}).get("instagram_api_expires")
        ig_token, new_expires = await refresh_token_if_needed(ig_token, expires_at)
        if new_expires:
            store.get("settings", {})["instagram_api_expires"] = new_expires
            store.get("settings", {})["instagram_api_token"] = ig_token
            save_store()

        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://graph.facebook.com/v19.0/{ig_id}",
                params={"fields": "id,username,followers_count", "access_token": ig_token},
                timeout=10,
            )
            data = r.json()

        if "error" in data:
            err = data["error"]
            store.get("settings", {})["instagram_api_connected"] = False
            save_store()
            return {
                "status": "error",
                "message": err.get("message", "Token invalid"),
                "error_code": err.get("code"),
                "reconnect_required": True,
            }

        add_log(f"✅ Instagram test OK: @{data.get('username')} ({data.get('followers_count',0)} followers)", "success")
        return {
            "status": "ok",
            "username": data.get("username"),
            "followers": data.get("followers_count", 0),
            "ig_id": ig_id,
            "message": f"Connected as @{data.get('username')}",
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "reconnect_required": True}

@router.post("/api/instagram/post/test")
async def instagram_post_test(req: Request):
    """Test Instagram connection by posting a test caption (no media — text-only test)"""
    from routes.auth import get_current_user
    from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
    token_store = store.get("settings", {}).get("instagram_api_token")
    ig_id = store.get("settings", {}).get("instagram_ig_id")
    connected = store.get("settings", {}).get("instagram_api_connected", False)

    if not token_store or not ig_id or not connected:
        return {
            "status": "error",
            "message": "Instagram not connected",
            "reconnect_required": True,
            "action": "Go to Connect page and reconnect Instagram"
        }

    try:
        from instagram_api import refresh_token_if_needed
        expires_at = store.get("settings", {}).get("instagram_api_expires")
        token, new_expires = await refresh_token_if_needed(token, expires_at)
        if new_expires:
            store.get("settings", {})["instagram_api_expires"] = new_expires
            store.get("settings", {})["instagram_api_token"] = token
            save_store()

        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"https://graph.facebook.com/v19.0/{ig_id}",
                params={"fields": "id,username,followers_count", "access_token": token},
                timeout=10,
            )
            data = r.json()

        if "error" in data:
            err = data["error"]
            store.get("settings", {})["instagram_api_connected"] = False
            save_store()
            return {
                "status": "error",
                "message": err.get("message", "Token invalid"),
                "error_code": err.get("code"),
                "reconnect_required": True,
            }

        add_log(f"✅ Instagram test OK: @{data.get('username')} ({data.get('followers_count',0)} followers)", "success")
        return {
            "status": "ok",
            "username": data.get("username"),
            "followers": data.get("followers_count", 0),
            "ig_id": ig_id,
            "message": f"Connected as @{data.get('username')}",
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "reconnect_required": True}
