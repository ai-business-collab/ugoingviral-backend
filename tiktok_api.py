"""
TikTok Content Posting API integration — UgoingViral
=====================================================
Håndterer OAuth flow og video/foto posting via TikTok Content Posting API v2.

Kræver:
- TikTok Developer App med scopes: video.publish, user.info.basic
- Brugeren skal forbinde sin TikTok konto via OAuth

API reference: https://developers.tiktok.com/doc/content-posting-api-get-started
"""

import httpx
import os
from datetime import datetime, timedelta
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
TIKTOK_CLIENT_KEY    = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI  = os.getenv("TIKTOK_REDIRECT_URI", "https://ugoingviral.com/api/tiktok/callback")

TIKTOK_AUTH_BASE = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_API_BASE  = "https://open.tiktokapis.com/v2"

SCOPES = ["user.info.basic", "video.publish", "video.list"]


# ── OAuth Flow ────────────────────────────────────────────────────────────────

def build_authorization_url(state: str = "") -> str:
    import urllib.parse
    params = {
        "client_key": TIKTOK_CLIENT_KEY,
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "state": state,
    }
    return TIKTOK_AUTH_BASE + "?" + urllib.parse.urlencode(params)


async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": TIKTOK_REDIRECT_URI,
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise Exception(data.get("error_description", data["error"]))
        # Add absolute expiry timestamps
        now = datetime.utcnow()
        data["access_token_expires_at"] = (
            now + timedelta(seconds=data.get("expires_in", 86400))
        ).isoformat()
        data["refresh_token_expires_at"] = (
            now + timedelta(seconds=data.get("refresh_expires_in", 2592000))
        ).isoformat()
        return data


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        r.raise_for_status()
        data = r.json()
        now = datetime.utcnow()
        data["access_token_expires_at"] = (
            now + timedelta(seconds=data.get("expires_in", 86400))
        ).isoformat()
        return data


async def refresh_token_if_needed(access_token: str, refresh_token: str, expires_at: Optional[str]) -> tuple[str, Optional[str], Optional[str]]:
    """Returns (access_token, new_refresh_token_or_None, new_expires_at_or_None)"""
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp - datetime.utcnow() < timedelta(hours=1):
                data = await refresh_access_token(refresh_token)
                return (
                    data["access_token"],
                    data.get("refresh_token", refresh_token),
                    data.get("access_token_expires_at"),
                )
        except Exception:
            pass
    return access_token, None, None


# ── Account Info ──────────────────────────────────────────────────────────────

async def get_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{TIKTOK_API_BASE}/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": "open_id,union_id,avatar_url,display_name,username"},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("data", {}).get("user", {})


# ── Creator Info (required before posting) ───────────────────────────────────

async def get_creator_info(access_token: str) -> dict:
    """Hent brugerens TikTok creator info inkl. privacy level options og max video duration"""
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{TIKTOK_API_BASE}/post/publish/creator_info/query/",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json; charset=UTF-8"},
            json={},
        )
        if r.status_code == 200:
            data = r.json()
            return data.get("data", {})
    return {}


# ── Video Publishing ──────────────────────────────────────────────────────────

async def post_video_from_url(
    access_token: str,
    video_url: str,
    title: str = "",
    privacy_level: str = "SELF_ONLY",
    disable_comment: bool = False,
    disable_duet: bool = False,
    disable_stitch: bool = False,
) -> dict:
    """
    Post video til TikTok via URL pull (ingen fil-upload nødvendigt).
    privacy_level: PUBLIC_TO_EVERYONE | MUTUAL_FOLLOW_FRIENDS | SELF_ONLY
    """
    payload = {
        "post_info": {
            "title": title[:2200] if title else "",
            "privacy_level": privacy_level,
            "disable_comment": disable_comment,
            "disable_duet": disable_duet,
            "disable_stitch": disable_stitch,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "video_url": video_url,
        },
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{TIKTOK_API_BASE}/post/publish/video/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=payload,
        )
        data = r.json()
        if r.status_code != 200 or data.get("error", {}).get("code") not in ("ok", None, ""):
            err = data.get("error", {})
            raise Exception(err.get("message", f"TikTok API error {r.status_code}"))
        return data.get("data", {})


async def get_publish_status(access_token: str, publish_id: str) -> dict:
    """Poll publicerings-status for en video."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={"publish_id": publish_id},
        )
        if r.status_code == 200:
            return r.json().get("data", {})
    return {}


async def post_photo(
    access_token: str,
    photo_urls: list,
    title: str = "",
    privacy_level: str = "SELF_ONLY",
) -> dict:
    """Post foto/carousel til TikTok (Photo Post API)."""
    payload = {
        "post_info": {
            "title": title[:2200] if title else "",
            "privacy_level": privacy_level,
            "disable_comment": False,
            "auto_add_music": True,
        },
        "source_info": {
            "source": "PULL_FROM_URL",
            "photo_cover_index": 0,
            "photo_images": photo_urls[:35],
        },
        "media_type": "PHOTO",
        "post_mode": "DIRECT_POST",
    }
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{TIKTOK_API_BASE}/post/publish/content/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=payload,
        )
        data = r.json()
        if r.status_code != 200 or data.get("error", {}).get("code") not in ("ok", None, ""):
            err = data.get("error", {})
            raise Exception(err.get("message", f"TikTok Photo API error {r.status_code}"))
        return data.get("data", {})


# ── High-level publish wrapper ────────────────────────────────────────────────

async def publish_to_tiktok(
    access_token: str,
    caption: str,
    video_url: Optional[str] = None,
    image_url: Optional[str] = None,
    image_urls: Optional[list] = None,
    privacy_level: str = "SELF_ONLY",
) -> dict:
    """
    Publicer til TikTok — vælger video eller foto baseret på input.
    Returns: {"status": "published"|"processing"|"error", "publish_id": ..., "message": ...}
    """
    try:
        if video_url:
            data = await post_video_from_url(
                access_token=access_token,
                video_url=video_url,
                title=caption,
                privacy_level=privacy_level,
            )
            publish_id = data.get("publish_id", "")
            return {"status": "processing", "publish_id": publish_id, "message": "Video sendes til TikTok — klar om lidt"}

        photos = image_urls or ([image_url] if image_url else [])
        if photos:
            data = await post_photo(
                access_token=access_token,
                photo_urls=photos,
                title=caption,
                privacy_level=privacy_level,
            )
            publish_id = data.get("publish_id", "")
            return {"status": "processing", "publish_id": publish_id, "message": "Foto sendes til TikTok"}

        return {"status": "error", "message": "Ingen video eller billede URL"}

    except Exception as e:
        return {"status": "error", "message": str(e)}
