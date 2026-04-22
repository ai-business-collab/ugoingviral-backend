"""
YouTube Data API v3 integration — UgoingViral
==============================================
OAuth2 flow, video upload og Shorts posting.

Kræver:
- Google Cloud Project med YouTube Data API v3 aktiveret
- OAuth 2.0 Client ID (Web application type)
- Scopes: youtube.upload, youtube.readonly

Env vars: YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET, YOUTUBE_REDIRECT_URI
"""

import httpx
import os
from datetime import datetime, timedelta
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
YOUTUBE_CLIENT_ID     = os.getenv("YOUTUBE_CLIENT_ID", "")
YOUTUBE_CLIENT_SECRET = os.getenv("YOUTUBE_CLIENT_SECRET", "")
YOUTUBE_REDIRECT_URI  = os.getenv("YOUTUBE_REDIRECT_URI", "https://ugoingviral.com/api/youtube/callback")

AUTH_URL   = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL  = "https://oauth2.googleapis.com/token"
API_BASE   = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

SCOPES = [
    "openid",
    "email",
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
]


# ── OAuth Flow ────────────────────────────────────────────────────────────────

def build_authorization_url(state: str = "") -> str:
    import urllib.parse
    params = {
        "client_id": YOUTUBE_CLIENT_ID,
        "redirect_uri": YOUTUBE_REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "response_type": "code",
        "access_type": "offline",   # Gets refresh_token
        "prompt": "consent",        # Always show consent to ensure refresh_token is returned
        "state": state,
    }
    return AUTH_URL + "?" + urllib.parse.urlencode(params)


async def exchange_code_for_token(code: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            TOKEN_URL,
            data={
                "client_id": YOUTUBE_CLIENT_ID,
                "client_secret": YOUTUBE_CLIENT_SECRET,
                "redirect_uri": YOUTUBE_REDIRECT_URI,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise Exception(data.get("error_description", data["error"]))
        now = datetime.utcnow()
        data["access_token_expires_at"] = (
            now + timedelta(seconds=data.get("expires_in", 3600))
        ).isoformat()
        return data


async def refresh_access_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            TOKEN_URL,
            data={
                "client_id": YOUTUBE_CLIENT_ID,
                "client_secret": YOUTUBE_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise Exception(data.get("error_description", data["error"]))
        now = datetime.utcnow()
        data["access_token_expires_at"] = (
            now + timedelta(seconds=data.get("expires_in", 3600))
        ).isoformat()
        return data


async def refresh_token_if_needed(
    access_token: str, refresh_token: str, expires_at: Optional[str]
) -> tuple[str, Optional[str]]:
    """Returns (access_token, new_expires_at_or_None)."""
    if expires_at and refresh_token:
        try:
            exp = datetime.fromisoformat(expires_at)
            if exp - datetime.utcnow() < timedelta(minutes=10):
                data = await refresh_access_token(refresh_token)
                return data["access_token"], data.get("access_token_expires_at")
        except Exception:
            pass
    return access_token, None


# ── Account Info ──────────────────────────────────────────────────────────────

async def get_channel_info(access_token: str) -> dict:
    """Hent brugerens YouTube kanal info."""
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{API_BASE}/channels",
            params={"part": "snippet,statistics", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        return items[0] if items else {}


# ── Video Upload ──────────────────────────────────────────────────────────────

async def upload_video_from_url(
    access_token: str,
    video_url: str,
    title: str,
    description: str = "",
    tags: Optional[list] = None,
    category_id: str = "22",
    privacy_status: str = "public",
    is_short: bool = False,
) -> dict:
    """
    Download video fra URL og upload til YouTube via resumable upload API.

    is_short=True tilføjer #Shorts til titel/beskrivelse for YouTube Shorts.
    privacy_status: 'public' | 'unlisted' | 'private'
    category_id: 22 = People & Blogs (standard for Shorts/content)
    """
    if is_short:
        if "#Shorts" not in title and "#shorts" not in title:
            title = title.rstrip() + " #Shorts"
        if "#Shorts" not in description and "#shorts" not in description:
            description = (description + "\n\n#Shorts").strip()

    metadata = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": (tags or [])[:500],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy_status,
            "selfDeclaredMadeForKids": False,
        },
    }

    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as c:
        # Trin 1: Download video fra URL
        dl = await c.get(video_url)
        dl.raise_for_status()
        video_bytes  = dl.content
        content_type = dl.headers.get("content-type", "video/mp4").split(";")[0].strip()
        if not content_type.startswith("video/"):
            content_type = "video/mp4"

        # Trin 2: Initialiser resumable upload — returnerer upload URI via Location header
        init_r = await c.post(
            f"{UPLOAD_URL}?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": content_type,
                "X-Upload-Content-Length": str(len(video_bytes)),
            },
            json=metadata,
        )
        if init_r.status_code not in (200, 201):
            err = {}
            try:
                err = init_r.json()
            except Exception:
                pass
            raise Exception(
                err.get("error", {}).get("message", f"YouTube init fejl {init_r.status_code}: {init_r.text[:200]}")
            )
        upload_uri = init_r.headers.get("Location", "")
        if not upload_uri:
            raise Exception("YouTube returnerede ingen upload URI i Location header")

        # Trin 3: Upload video bytes til upload URI
        up_r = await c.put(
            upload_uri,
            content=video_bytes,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(video_bytes)),
            },
        )
        if up_r.status_code not in (200, 201):
            raise Exception(f"YouTube video upload fejlede: HTTP {up_r.status_code}")

        data = up_r.json()
        video_id = data.get("id", "")
        return {
            "status":   "published",
            "video_id": video_id,
            "url":      f"https://youtube.com/watch?v={video_id}",
            "title":    title,
            "is_short": is_short,
        }


# ── High-level publish wrapper ────────────────────────────────────────────────

async def publish_to_youtube(
    access_token: str,
    title: str,
    description: str = "",
    video_url: Optional[str] = None,
    tags: Optional[list] = None,
    privacy_status: str = "public",
    is_short: bool = False,
) -> dict:
    """
    Publicer video til YouTube.
    Returns: {"status": "published"|"error", "video_id": ..., "url": ..., "message": ...}
    """
    try:
        if not video_url:
            return {"status": "error", "message": "Ingen video URL — YouTube kræver en video"}
        return await upload_video_from_url(
            access_token=access_token,
            video_url=video_url,
            title=title,
            description=description,
            tags=tags,
            privacy_status=privacy_status,
            is_short=is_short,
        )
    except Exception as e:
        return {"status": "error", "message": str(e)}
