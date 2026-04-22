"""
Instagram Graph API integration — UgoingViral
=============================================
Håndterer OAuth flow, token management og content publishing
via Metas officielle Instagram Graph API.

Kræver:
- Instagram Business eller Creator konto
- Facebook side forbundet til Instagram
- Meta App med Instagram Graph API produkt

Endpoints bygget her:
- OAuth start → redirect til Meta login
- OAuth callback → exchange code for token
- Post billede/video til Instagram
- Hent konto info og metrics
"""

import httpx
import json
import os
from datetime import datetime, timedelta
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
INSTAGRAM_APP_ID     = os.getenv("INSTAGRAM_APP_ID", "984993797518771")
INSTAGRAM_APP_SECRET = os.getenv("INSTAGRAM_APP_SECRET", "")
INSTAGRAM_REDIRECT   = os.getenv("INSTAGRAM_REDIRECT_URI", "http://localhost:8000/api/instagram/callback")

GRAPH_BASE = "https://graph.facebook.com/v19.0"

# Permissions vi skal bruge
SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_messages",
    "instagram_business_manage_comments",
]

# ── OAuth Flow ────────────────────────────────────────────────────────────────

def build_authorization_url(state: str = "") -> str:
    """Byg OAuth URL som brugeren sendes til for at give tilladelse"""
    import urllib.parse
    params = {
        "client_id": INSTAGRAM_APP_ID,
        "redirect_uri": INSTAGRAM_REDIRECT,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "state": state,
    }
    return "https://www.facebook.com/v19.0/dialog/oauth?" + urllib.parse.urlencode(params)


async def exchange_code_for_token(code: str) -> dict:
    """Byt OAuth code for short-lived access token"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{GRAPH_BASE}/oauth/access_token",
            data={
                "client_id": INSTAGRAM_APP_ID,
                "client_secret": INSTAGRAM_APP_SECRET,
                "redirect_uri": INSTAGRAM_REDIRECT,
                "code": code,
            }
        )
        r.raise_for_status()
        return r.json()


async def get_long_lived_token(short_token: str) -> dict:
    """Konverter short-lived token (1 time) til long-lived token (60 dage)"""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{GRAPH_BASE}/oauth/access_token",
            params={
                "grant_type": "fb_exchange_token",
                "client_id": INSTAGRAM_APP_ID,
                "client_secret": INSTAGRAM_APP_SECRET,
                "fb_exchange_token": short_token,
            }
        )
        r.raise_for_status()
        data = r.json()
        # Beregn udløbstid
        expires_in = data.get("expires_in", 5184000)  # 60 dage default
        data["expires_at"] = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        return data


async def refresh_token_if_needed(token: str, expires_at: Optional[str]) -> tuple[str, Optional[str]]:
    """Forny token automatisk hvis det udløber inden for 7 dage"""
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp - datetime.utcnow() < timedelta(days=7):
                data = await get_long_lived_token(token)
                return data["access_token"], data.get("expires_at")
        except: pass
    return token, expires_at


# ── Account Info ──────────────────────────────────────────────────────────────

async def get_instagram_account_id(access_token: str) -> Optional[str]:
    """Hent Instagram Business Account ID fra Facebook user"""
    async with httpx.AsyncClient() as c:
        # Hent Facebook sider brugeren administrerer
        r = await c.get(
            f"{GRAPH_BASE}/me/accounts",
            params={"access_token": access_token, "fields": "id,name,instagram_business_account"}
        )
        r.raise_for_status()
        data = r.json()
        pages = data.get("data", [])
        for page in pages:
            ig = page.get("instagram_business_account")
            if ig:
                return ig.get("id")
    return None


async def get_account_info(ig_account_id: str, access_token: str) -> dict:
    """Hent Instagram konto info og metrics"""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{GRAPH_BASE}/{ig_account_id}",
            params={
                "access_token": access_token,
                "fields": "id,username,name,biography,followers_count,follows_count,media_count,profile_picture_url,website"
            }
        )
        r.raise_for_status()
        return r.json()


# ── Publishing ────────────────────────────────────────────────────────────────

async def create_image_container(ig_account_id: str, access_token: str, image_url: str, caption: str) -> str:
    """Trin 1: Opret media container til billede"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{GRAPH_BASE}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "image_url": image_url,
                "caption": caption,
            }
        )
        r.raise_for_status()
        return r.json()["id"]


async def create_video_container(ig_account_id: str, access_token: str, video_url: str, caption: str, media_type: str = "REELS") -> str:
    """Trin 1: Opret media container til video/reels"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{GRAPH_BASE}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "video_url": video_url,
                "caption": caption,
                "media_type": media_type,
            }
        )
        r.raise_for_status()
        return r.json()["id"]


async def create_carousel_container(ig_account_id: str, access_token: str, image_urls: list, caption: str) -> str:
    """Trin 1+2: Opret carousel med flere billeder"""
    async with httpx.AsyncClient() as c:
        # Opret individuelle media items
        children = []
        for url in image_urls[:10]:  # Max 10 billeder i carousel
            r = await c.post(
                f"{GRAPH_BASE}/{ig_account_id}/media",
                params={
                    "access_token": access_token,
                    "image_url": url,
                    "is_carousel_item": "true",
                }
            )
            r.raise_for_status()
            children.append(r.json()["id"])

        # Opret carousel container
        r = await c.post(
            f"{GRAPH_BASE}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
            }
        )
        r.raise_for_status()
        return r.json()["id"]


async def wait_for_container_ready(container_id: str, access_token: str, max_wait: int = 60) -> bool:
    """Vent til media container er klar til publicering (video upload kan tage tid)"""
    import asyncio
    for _ in range(max_wait // 5):
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{GRAPH_BASE}/{container_id}",
                params={"access_token": access_token, "fields": "status_code"}
            )
            status = r.json().get("status_code", "")
            if status == "FINISHED":
                return True
            if status == "ERROR":
                return False
        await asyncio.sleep(5)
    return False


async def publish_container(ig_account_id: str, access_token: str, container_id: str) -> dict:
    """Trin 2: Publiser media container"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{GRAPH_BASE}/{ig_account_id}/media_publish",
            params={
                "access_token": access_token,
                "creation_id": container_id,
            }
        )
        r.raise_for_status()
        return r.json()


async def post_to_instagram(
    ig_account_id: str,
    access_token: str,
    caption: str,
    image_url: Optional[str] = None,
    image_urls: Optional[list] = None,
    video_url: Optional[str] = None,
    media_type: str = "IMAGE"
) -> dict:
    """
    Komplet post flow — vælger automatisk rigtig container type.
    
    Returns: {"id": "post_id", "status": "published"}
    """
    try:
        # Vælg container type
        if image_urls and len(image_urls) > 1:
            container_id = await create_carousel_container(ig_account_id, access_token, image_urls, caption)
        elif video_url or media_type in ("REELS", "VIDEO"):
            url = video_url or image_url
            container_id = await create_video_container(ig_account_id, access_token, url, caption, media_type)
            # Vent til video er klar
            ready = await wait_for_container_ready(container_id, access_token)
            if not ready:
                return {"status": "error", "message": "Video container timeout"}
        else:
            url = image_url or (image_urls[0] if image_urls else None)
            if not url:
                return {"status": "error", "message": "Ingen billede URL"}
            container_id = await create_image_container(ig_account_id, access_token, url, caption)

        # Publiser
        result = await publish_container(ig_account_id, access_token, container_id)
        return {"status": "published", "id": result.get("id"), "container_id": container_id}

    except httpx.HTTPStatusError as e:
        error_data = {}
        try: error_data = e.response.json()
        except: pass
        msg = error_data.get("error", {}).get("message", str(e))
        return {"status": "error", "message": msg, "code": e.response.status_code}
    except Exception as e:
        return {"status": "error", "message": str(e)}


# ── Analytics ─────────────────────────────────────────────────────────────────

async def get_post_insights(post_id: str, access_token: str) -> dict:
    """Hent metrics for et enkelt opslag"""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{GRAPH_BASE}/{post_id}/insights",
            params={
                "access_token": access_token,
                "metric": "impressions,reach,likes,comments,shares,saves,video_views"
            }
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            return {item["name"]: item["values"][0]["value"] for item in data if item.get("values")}
        return {}


async def get_account_insights(ig_account_id: str, access_token: str, period: str = "day") -> dict:
    """Hent konto-level metrics"""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{GRAPH_BASE}/{ig_account_id}/insights",
            params={
                "access_token": access_token,
                "metric": "impressions,reach,follower_count,profile_views",
                "period": period,
            }
        )
        if r.status_code == 200:
            data = r.json().get("data", [])
            return {item["name"]: item["values"][-1]["value"] for item in data if item.get("values")}
        return {}
