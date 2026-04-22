from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ══════════════════════════════════════════════════════════

@router.get("/api/instagram/oauth/start")
async def instagram_oauth_start():
    """Start Instagram OAuth flow"""
    try:
        from instagram_api import build_authorization_url
        import base64, json as _json
        state = base64.urlsafe_b64encode(_json.dumps({"platform": "instagram"}).encode()).decode()
        url = build_authorization_url(state)
        return {"authorization_url": url, "state": state}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@router.get("/api/instagram/callback")
async def instagram_oauth_callback(code: str = "", state: str = "", error: str = ""):
    """Modtag OAuth callback fra Meta"""
    if error:
        return {"status": "error", "message": error}
    if not code:
        return {"status": "error", "message": "Ingen code modtaget"}
    try:
        from instagram_api import exchange_code_for_token, get_long_lived_token, get_instagram_account_id, get_account_info
        # Exchange code for token
        token_data = await exchange_code_for_token(code)
        short_token = token_data.get("access_token")
        if not short_token:
            return {"status": "error", "message": "Ingen access token"}
        # Konverter til long-lived token
        long_data = await get_long_lived_token(short_token)
        long_token = long_data.get("access_token", short_token)
        expires_at = long_data.get("expires_at")
        # Hent Instagram account ID
        ig_id = await get_instagram_account_id(long_token)
        if not ig_id:
            return {"status": "error", "message": "Ingen Instagram Business konto fundet — forbind Instagram til en Facebook side"}
        # Hent konto info
        info = await get_account_info(ig_id, long_token)
        # Gem i store
        store["settings"]["instagram_api_token"] = long_token
        store["settings"]["instagram_api_expires"] = expires_at
        store["settings"]["instagram_ig_id"] = ig_id
        store["settings"]["instagram_username"] = info.get("username", "")
        store["settings"]["instagram_api_connected"] = True
        save_store()
        add_log(f"✅ Instagram API forbundet: @{info.get('username', ig_id)}", "success")
        # Redirect til app
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/app?instagram_connected=1")
    except Exception as e:
        add_log(f"❌ Instagram OAuth fejl: {str(e)[:100]}", "error")
        return {"status": "error", "message": str(e)}

@router.get("/api/instagram/status")
async def instagram_api_status():
    """Tjek om Instagram API er forbundet"""
    connected = store["settings"].get("instagram_api_connected", False)
    username = store["settings"].get("instagram_username", "")
    ig_id = store["settings"].get("instagram_ig_id", "")
    expires = store["settings"].get("instagram_api_expires", "")
    return {
        "connected": connected,
        "username": username,
        "ig_id": ig_id,
        "expires_at": expires,
        "mode": "api" if connected else "playwright"
    }

@router.post("/api/instagram/post")
async def instagram_api_post(req: Request):
    """Post til Instagram via officiel API"""
    d = await req.json()
    caption = d.get("caption", "")
    image_url = d.get("image_url")
    image_urls = d.get("image_urls", [])
    video_url = d.get("video_url")
    media_type = d.get("media_type", "IMAGE")

    token = store["settings"].get("instagram_api_token")
    ig_id = store["settings"].get("instagram_ig_id")

    if not token or not ig_id:
        return {"status": "error", "message": "Instagram API ikke forbundet — gå til Connect"}

    try:
        from instagram_api import post_to_instagram, refresh_token_if_needed
        # Forny token hvis nødvendigt
        expires_at = store["settings"].get("instagram_api_expires")
        token, new_expires = await refresh_token_if_needed(token, expires_at)
        if new_expires:
            store["settings"]["instagram_api_expires"] = new_expires
            store["settings"]["instagram_api_token"] = token
            save_store()

        result = await post_to_instagram(
            ig_id, token, caption,
            image_url=image_url,
            image_urls=image_urls if image_urls else None,
            video_url=video_url,
            media_type=media_type
        )
        if result.get("status") == "published":
            add_log(f"✅ Instagram API: opslag postet! ID: {result.get('id')}", "success")
        else:
            add_log(f"❌ Instagram API fejl: {result.get('message')}", "error")
        return result
    except Exception as e:
        add_log(f"❌ Instagram post fejl: {str(e)[:100]}", "error")
        return {"status": "error", "message": str(e)}

@router.get("/api/instagram/insights/{post_id}")
async def instagram_post_insights(post_id: str):
    """Hent metrics for et opslag"""
    token = store["settings"].get("instagram_api_token")
    if not token:
        return {"status": "error", "message": "Instagram API ikke forbundet"}
    from instagram_api import get_post_insights
    return await get_post_insights(post_id, token)

@router.get("/api/instagram/account/insights")
async def instagram_account_insights():
    """Hent konto metrics"""
    token = store["settings"].get("instagram_api_token")
    ig_id = store["settings"].get("instagram_ig_id")
    if not token or not ig_id:
        return {"status": "error", "message": "Instagram API ikke forbundet"}
    from instagram_api import get_account_insights
    return await get_account_insights(ig_id, token)

# ══════════════════════════════════════════════════════════
# SCHEDULER — kører automatisk i baggrunden