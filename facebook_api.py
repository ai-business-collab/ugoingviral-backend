"""
Facebook Graph API integration — UgoingViral
============================================
Officiel Facebook posting via Metas Graph API i stedet for browser automation.

Kræver:
- En Facebook Page (opslag postes til siden, ikke en privat profil)
- En Page Access Token i FACEBOOK_ACCESS_TOKEN (.env)

Endpoints brugt her:
- POST /me/photos   → billede-opslag
- POST /me/videos   → video-opslag
- POST /me/feed     → ren tekst-opslag

Returnerer altid {status, id, post_id, url, message} så kalderen kan vise
et success-banner med et klikbart link til det live opslag.
"""

import os
import httpx
from typing import Optional

GRAPH_BASE = "https://graph.facebook.com/v19.0"


def get_access_token() -> str:
    """Hent Facebook Page Access Token fra miljøet (.env)."""
    return os.getenv("FACEBOOK_ACCESS_TOKEN", "").strip()


def _post_url(post_id: str) -> str:
    """Byg et offentligt URL til et Facebook-opslag ud fra dets id."""
    if not post_id:
        return ""
    return f"https://www.facebook.com/{post_id}"


def _video_url(video_id: str) -> str:
    if not video_id:
        return ""
    return f"https://www.facebook.com/watch/?v={video_id}"


def _extract_error(resp: httpx.Response) -> str:
    """Træk en brugbar fejlbesked ud af et Graph API-svar."""
    try:
        j = resp.json()
        err = j.get("error", {})
        return err.get("message") or err.get("error_user_msg") or str(j)
    except Exception:
        return (resp.text or "")[:200]


async def post_image(access_token: str, image_url: str, caption: str = "") -> dict:
    """Post et billede til Facebook via POST /me/photos."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{GRAPH_BASE}/me/photos",
            data={
                "url": image_url,
                "caption": caption or "",
                "access_token": access_token,
            },
        )
    if r.status_code in (200, 201):
        j = r.json()
        # /me/photos returnerer photo "id" og opslagets "post_id".
        post_id = j.get("post_id") or j.get("id", "")
        return {
            "status": "published",
            "id": j.get("id", ""),
            "post_id": post_id,
            "url": _post_url(post_id),
        }
    return {"status": "error", "message": f"Facebook API {r.status_code}: {_extract_error(r)}"}


async def post_video(access_token: str, video_url: str, caption: str = "") -> dict:
    """Post en video til Facebook via POST /me/videos."""
    async with httpx.AsyncClient(timeout=120) as c:
        r = await c.post(
            f"{GRAPH_BASE}/me/videos",
            data={
                "file_url": video_url,
                "description": caption or "",
                "access_token": access_token,
            },
        )
    if r.status_code in (200, 201):
        j = r.json()
        vid = j.get("id", "")
        return {
            "status": "published",
            "id": vid,
            "post_id": vid,
            "url": _video_url(vid),
        }
    return {"status": "error", "message": f"Facebook API {r.status_code}: {_extract_error(r)}"}


async def post_text(access_token: str, message: str) -> dict:
    """Post en ren tekst-opdatering til Facebook via POST /me/feed."""
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{GRAPH_BASE}/me/feed",
            data={"message": message or "", "access_token": access_token},
        )
    if r.status_code in (200, 201):
        post_id = r.json().get("id", "")
        return {"status": "published", "id": post_id, "post_id": post_id, "url": _post_url(post_id)}
    return {"status": "error", "message": f"Facebook API {r.status_code}: {_extract_error(r)}"}


async def post_to_facebook(
    caption: str = "",
    image_url: Optional[str] = None,
    video_url: Optional[str] = None,
    access_token: Optional[str] = None,
) -> dict:
    """
    Komplet Facebook post-flow — vælger automatisk billede/video/tekst.

    Returns: {status, id, post_id, url, message}
    """
    token = (access_token or get_access_token()).strip()
    if not token:
        return {"status": "error", "message": "FACEBOOK_ACCESS_TOKEN mangler i .env"}

    try:
        if video_url:
            return await post_video(token, video_url, caption)
        if image_url:
            return await post_image(token, image_url, caption)
        if caption:
            return await post_text(token, caption)
        return {"status": "error", "message": "Facebook kræver tekst, billede eller video"}
    except httpx.HTTPError as e:
        return {"status": "error", "message": f"Facebook netværksfejl: {e}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}
