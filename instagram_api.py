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

# ── Facebook Login (linked Page) permissions ───────────────────────────────────
# CRITICAL: publishing needs instagram_content_publish; insights need
# instagram_manage_insights; reading the linked IG account needs instagram_basic.
# The old list (public_profile/pages_show_list/business_management) could FIND the
# account but not post or read insights — Meta review would (and did) reject it.
SCOPES = [
    "public_profile",
    "pages_show_list",
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_insights",
    "instagram_manage_comments",
    "pages_read_engagement",
    "pages_manage_posts",
]

# ── Instagram Login API (direct, no Facebook Page) ─────────────────────────────
# "Instagram API with Instagram Login": the user logs in with Instagram only.
# Separate Instagram-app credentials (set these in .env; falls back to the FB app
# id/secret only so the module imports cleanly — real IG-Login needs its own app).
IG_LOGIN_APP_ID     = os.getenv("INSTAGRAM_LOGIN_APP_ID", "") or INSTAGRAM_APP_ID
IG_LOGIN_APP_SECRET = os.getenv("INSTAGRAM_LOGIN_APP_SECRET", "") or INSTAGRAM_APP_SECRET
IG_LOGIN_REDIRECT   = os.getenv("INSTAGRAM_LOGIN_REDIRECT_URI",
                                "http://localhost:8000/api/instagram/login/callback")
IG_GRAPH_BASE = "https://graph.instagram.com/v21.0"      # publishing/comments base for IG Login
IG_LOGIN_SCOPES = [
    "instagram_business_basic",
    "instagram_business_content_publish",
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


# ── Instagram Login API (direct login, no Facebook Page) ───────────────────────

def build_instagram_login_url(state: str = "") -> str:
    """OAuth URL for logging in with Instagram directly (no Facebook Page)."""
    import urllib.parse
    params = {
        "client_id": IG_LOGIN_APP_ID,
        "redirect_uri": IG_LOGIN_REDIRECT,
        "scope": ",".join(IG_LOGIN_SCOPES),
        "response_type": "code",
        "state": state,
    }
    return "https://www.instagram.com/oauth/authorize?" + urllib.parse.urlencode(params)


async def ig_login_exchange_code(code: str) -> dict:
    """Exchange the code for a short-lived Instagram-Login token. Returns
    {access_token, user_id, permissions}."""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.instagram.com/oauth/access_token",
            data={
                "client_id": IG_LOGIN_APP_ID,
                "client_secret": IG_LOGIN_APP_SECRET,
                "grant_type": "authorization_code",
                "redirect_uri": IG_LOGIN_REDIRECT,
                "code": code,
            },
        )
        r.raise_for_status()
        return r.json()


async def ig_login_long_lived(short_token: str) -> dict:
    """Convert a short-lived Instagram-Login token to a 60-day long-lived one."""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            "https://graph.instagram.com/access_token",
            params={"grant_type": "ig_exchange_token",
                    "client_secret": IG_LOGIN_APP_SECRET, "access_token": short_token},
        )
        r.raise_for_status()
        data = r.json()
        expires_in = data.get("expires_in", 5184000)
        data["expires_at"] = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        return data


async def ig_login_refresh_if_needed(token: str, expires_at: Optional[str]) -> tuple[str, Optional[str]]:
    """Refresh an Instagram-Login long-lived token when it's within 7 days of expiry."""
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) - datetime.utcnow() < timedelta(days=7):
                async with httpx.AsyncClient() as c:
                    r = await c.get("https://graph.instagram.com/refresh_access_token",
                                    params={"grant_type": "ig_refresh_token", "access_token": token})
                    r.raise_for_status()
                    d = r.json()
                    exp = (datetime.utcnow() + timedelta(seconds=d.get("expires_in", 5184000))).isoformat()
                    return d.get("access_token", token), exp
        except Exception:
            pass
    return token, expires_at


async def ig_login_account_info(access_token: str) -> dict:
    """Fetch the logged-in Instagram professional account's own profile
    (graph.instagram.com/me). Returns id + username etc."""
    async with httpx.AsyncClient() as c:
        r = await c.get(f"{IG_GRAPH_BASE}/me",
                        params={"access_token": access_token,
                                "fields": "id,username,account_type,media_count"})
        r.raise_for_status()
        return r.json()


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


async def get_media_permalink(media_id: str, access_token: str) -> str:
    """Fetch the public permalink (post URL) for a published media id."""
    if not media_id:
        return ""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(
                f"{GRAPH_BASE}/{media_id}",
                params={"access_token": access_token, "fields": "permalink"},
            )
            if r.status_code == 200:
                return r.json().get("permalink", "") or ""
    except Exception:
        pass
    return ""


async def get_user_media(ig_account_id: str, access_token: str, limit: int = 20) -> list:
    """List the connected Instagram Business/Creator account's own posts.

    Returns a list of dicts: {id, caption, media_type, thumbnail, permalink,
    timestamp, like_count, comments_count}.
    """
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(
            f"{GRAPH_BASE}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "fields": "id,caption,media_type,media_url,thumbnail_url,permalink,timestamp,like_count,comments_count",
                "limit": max(1, min(int(limit or 20), 50)),
            },
        )
        r.raise_for_status()
        out = []
        for m in r.json().get("data", []):
            # Videos expose a thumbnail_url; images use media_url.
            thumb = m.get("thumbnail_url") or m.get("media_url") or ""
            out.append({
                "id": m.get("id", ""),
                "caption": m.get("caption", "") or "",
                "media_type": m.get("media_type", ""),
                "thumbnail": thumb,
                "permalink": m.get("permalink", ""),
                "timestamp": m.get("timestamp", ""),
                "like_count": int(m.get("like_count", 0) or 0),
                "comments_count": int(m.get("comments_count", 0) or 0),
            })
        return out


# ── Publishing ────────────────────────────────────────────────────────────────

async def create_image_container(ig_account_id: str, access_token: str, image_url: str, caption: str, base: str = GRAPH_BASE) -> str:
    """Trin 1: Opret media container til billede"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{base}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "image_url": image_url,
                "caption": caption,
            }
        )
        r.raise_for_status()
        return r.json()["id"]


async def create_video_container(ig_account_id: str, access_token: str, video_url: str, caption: str, media_type: str = "REELS", base: str = GRAPH_BASE) -> str:
    """Trin 1: Opret media container til video/reels"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{base}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "video_url": video_url,
                "caption": caption,
                "media_type": media_type,
            }
        )
        r.raise_for_status()
        return r.json()["id"]


async def create_carousel_container(ig_account_id: str, access_token: str, image_urls: list, caption: str, base: str = GRAPH_BASE) -> str:
    """Trin 1+2: Opret carousel med flere billeder"""
    async with httpx.AsyncClient() as c:
        # Opret individuelle media items
        children = []
        for url in image_urls[:10]:  # Max 10 billeder i carousel
            r = await c.post(
                f"{base}/{ig_account_id}/media",
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
            f"{base}/{ig_account_id}/media",
            params={
                "access_token": access_token,
                "media_type": "CAROUSEL",
                "children": ",".join(children),
                "caption": caption,
            }
        )
        r.raise_for_status()
        return r.json()["id"]


async def wait_for_container_ready(container_id: str, access_token: str, max_wait: int = 60, base: str = GRAPH_BASE) -> bool:
    """Vent til media container er klar til publicering (video upload kan tage tid)"""
    import asyncio
    for _ in range(max_wait // 5):
        async with httpx.AsyncClient() as c:
            r = await c.get(
                f"{base}/{container_id}",
                params={"access_token": access_token, "fields": "status_code"}
            )
            status = r.json().get("status_code", "")
            if status == "FINISHED":
                return True
            if status == "ERROR":
                return False
        await asyncio.sleep(5)
    return False


async def publish_container(ig_account_id: str, access_token: str, container_id: str, base: str = GRAPH_BASE) -> dict:
    """Trin 2: Publiser media container"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{base}/{ig_account_id}/media_publish",
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
    media_type: str = "IMAGE",
    base: str = GRAPH_BASE,
) -> dict:
    """
    Komplet post flow — vælger automatisk rigtig container type.
    `base` routes the same publish flow to graph.facebook.com (Facebook Login) or
    graph.instagram.com (Instagram Login). Returns {"id","status":"published"}.
    """
    try:
        # Vælg container type
        if image_urls and len(image_urls) > 1:
            container_id = await create_carousel_container(ig_account_id, access_token, image_urls, caption, base=base)
        elif video_url or media_type in ("REELS", "VIDEO"):
            url = video_url or image_url
            container_id = await create_video_container(ig_account_id, access_token, url, caption, media_type, base=base)
            # Vent til video er klar
            ready = await wait_for_container_ready(container_id, access_token, base=base)
            if not ready:
                return {"status": "error", "message": "Video container timeout"}
        else:
            url = image_url or (image_urls[0] if image_urls else None)
            if not url:
                return {"status": "error", "message": "Ingen billede URL"}
            container_id = await create_image_container(ig_account_id, access_token, url, caption, base=base)

        # Publiser
        result = await publish_container(ig_account_id, access_token, container_id, base=base)
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
