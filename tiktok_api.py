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
import json as _json
import logging
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger("tiktok_api")
# The app configures no root logging handler, so INFO would otherwise be dropped.
# Attach our own stdout handler so the exact TikTok payloads are visible in the
# service journal (journalctl -u ugoingviral) for debugging post failures.
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s [tiktok_api] %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False

# ── Config ────────────────────────────────────────────────────────────────────
TIKTOK_CLIENT_KEY    = os.getenv("TIKTOK_CLIENT_KEY", "")
TIKTOK_CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI  = os.getenv("TIKTOK_REDIRECT_URI", "https://ugoingviral.com/api/tiktok/callback")

TIKTOK_AUTH_BASE = "https://www.tiktok.com/v2/auth/authorize/"
TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
TIKTOK_API_BASE  = "https://open.tiktokapis.com/v2"

# Sandbox / unaudited apps may ONLY post privately. When true, every post is
# forced to SELF_ONLY regardless of the requested privacy level. Flip to "false"
# in .env once the TikTok app passes audit and PUBLIC posting is approved.
TIKTOK_SANDBOX = os.getenv("TIKTOK_SANDBOX", "true").strip().lower() not in ("false", "0", "no", "")

# The only privacy levels TikTok's Content Posting API accepts. Sending anything
# else (or an empty/None value) makes the API reject the whole request with
# `invalid_params: "The request post info is empty or incorrect"`.
VALID_PRIVACY_LEVELS = {
    "PUBLIC_TO_EVERYONE",
    "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWER_OF_CREATOR",
    "SELF_ONLY",
}

# Friendly aliases callers/older frontends might send → canonical TikTok value.
_PRIVACY_ALIASES = {
    "PUBLIC": "PUBLIC_TO_EVERYONE",
    "EVERYONE": "PUBLIC_TO_EVERYONE",
    "PRIVATE": "SELF_ONLY",
    "ONLY_ME": "SELF_ONLY",
    "SELF": "SELF_ONLY",
    "FRIENDS": "MUTUAL_FOLLOW_FRIENDS",
    "MUTUAL": "MUTUAL_FOLLOW_FRIENDS",
    "FOLLOWERS": "FOLLOWER_OF_CREATOR",
}


def normalize_privacy_level(level: Optional[str]) -> str:
    """Coerce any caller-supplied privacy value into a TikTok-valid one.

    Guarantees post_info.privacy_level is never empty/None/invalid — the root
    cause of TikTok's "request post info is empty or incorrect" error. In
    sandbox mode (TIKTOK_SANDBOX), always returns SELF_ONLY since unaudited
    apps cannot post publicly.
    """
    if TIKTOK_SANDBOX:
        return "SELF_ONLY"
    key = (level or "").strip().upper()
    if key in VALID_PRIVACY_LEVELS:
        return key
    if key in _PRIVACY_ALIASES:
        return _PRIVACY_ALIASES[key]
    return "SELF_ONLY"

SCOPES = [
    "user.info.basic",    # username, avatar, open_id
    "user.info.profile",  # bio_description, profile_deep_link, is_verified
    "user.info.stats",    # follower_count, following_count, likes_count, video_count
    "video.list",         # list the creator's own videos
    "video.upload",       # upload video to the creator's inbox (draft)
    "video.publish",      # direct post a video to the creator's profile
]


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

# All fields across user.info.basic + user.info.profile + user.info.stats. TikTok
# returns only the fields the granted scopes allow, so requesting the full set is
# safe — missing scopes simply omit their fields rather than erroring.
USER_INFO_FIELDS = (
    "open_id,union_id,avatar_url,avatar_url_100,avatar_large_url,"  # basic
    "display_name,bio_description,profile_deep_link,is_verified,username,"  # profile
    "follower_count,following_count,likes_count,video_count"  # stats
)


async def get_user_info(access_token: str, fields: str = USER_INFO_FIELDS) -> dict:
    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{TIKTOK_API_BASE}/user/info/",
            headers={"Authorization": f"Bearer {access_token}"},
            params={"fields": fields},
        )
        r.raise_for_status()
        data = r.json()
        return data.get("data", {}).get("user", {})


# ── Video list (video.list scope) ──────────────────────────────────────────────

VIDEO_LIST_FIELDS = (
    "id,title,video_description,duration,cover_image_url,embed_link,share_url,"
    "view_count,like_count,comment_count,share_count,create_time"
)


async def get_user_videos(access_token: str, cursor: int = 0, max_count: int = 20) -> dict:
    """List the connected creator's own TikTok videos (video.list scope).
    Returns {"videos": [...], "cursor": int, "has_more": bool}."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{TIKTOK_API_BASE}/video/list/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            params={"fields": VIDEO_LIST_FIELDS},
            json={"max_count": max(1, min(int(max_count or 20), 20)), "cursor": int(cursor or 0)},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("error", {}).get("code") not in ("ok", None, ""):
            raise Exception(data.get("error", {}).get("message", "TikTok video.list error"))
        d = data.get("data", {})
        return {
            "videos":   d.get("videos", []) or [],
            "cursor":   d.get("cursor", 0),
            "has_more": bool(d.get("has_more", False)),
        }


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
    privacy_level = normalize_privacy_level(privacy_level)
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
    logger.info("TikTok VIDEO init payload -> %s", _json.dumps(payload, ensure_ascii=False))
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


def _build_post_url(post_id: str, username: str = "") -> str:
    """Build a public TikTok video URL from a post id (and handle if known)."""
    if not post_id:
        return ""
    handle = (username or "").lstrip("@").strip()
    if handle:
        return f"https://www.tiktok.com/@{handle}/video/{post_id}"
    return f"https://www.tiktok.com/video/{post_id}"


async def resolve_post_url(
    access_token: str,
    publish_id: str,
    username: str = "",
    max_attempts: int = 6,
    delay: float = 2.0,
) -> dict:
    """Poll publish status until the post is live, then return its public URL.

    Returns {"status": <last_status>, "post_url": <url or "">, "post_id": <id or "">}.
    For SELF_ONLY / private posts TikTok may not expose a public id — in that case
    post_url is "" and the caller falls back to the creator's profile link.
    """
    import asyncio
    last_status = ""
    for attempt in range(max_attempts):
        data = await get_publish_status(access_token, publish_id)
        last_status = data.get("status", "") or last_status
        # TikTok ships this field name misspelled ("publicaly"); accept both.
        ids = (data.get("publicaly_available_post_id")
               or data.get("publicly_available_post_id") or [])
        if ids:
            pid = str(ids[0])
            return {"status": last_status or "PUBLISH_COMPLETE", "post_url": _build_post_url(pid, username), "post_id": pid}
        if last_status in ("PUBLISH_COMPLETE", "FAILED"):
            break
        if attempt < max_attempts - 1:
            await asyncio.sleep(delay)
    return {"status": last_status, "post_url": "", "post_id": ""}


async def post_photo(
    access_token: str,
    photo_urls: list,
    title: str = "",
    privacy_level: str = "SELF_ONLY",
) -> dict:
    """Post foto/carousel til TikTok (Photo Post API)."""
    privacy_level = normalize_privacy_level(privacy_level)
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
    logger.info("TikTok PHOTO init payload -> %s", _json.dumps(payload, ensure_ascii=False))
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
    wait_for_url: bool = False,
    username: str = "",
) -> dict:
    """
    Publicer til TikTok — vælger video eller foto baseret på input.
    Returns: {"status": "published"|"processing"|"error", "publish_id": ...,
              "message": ..., "post_url": <public url if resolved>}

    When wait_for_url is True (interactive posting), the publish status is polled
    so the caller can show the live TikTok post URL. Background callers (scheduler)
    leave it False to avoid blocking.
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
            result = {"status": "processing", "publish_id": publish_id, "message": "Video sendes til TikTok — klar om lidt", "post_url": ""}
        else:
            photos = image_urls or ([image_url] if image_url else [])
            if not photos:
                return {"status": "error", "message": "Ingen video eller billede URL"}
            data = await post_photo(
                access_token=access_token,
                photo_urls=photos,
                title=caption,
                privacy_level=privacy_level,
            )
            publish_id = data.get("publish_id", "")
            result = {"status": "processing", "publish_id": publish_id, "message": "Foto sendes til TikTok", "post_url": ""}

        if wait_for_url and result.get("publish_id"):
            try:
                info = await resolve_post_url(access_token, result["publish_id"], username=username)
                if info.get("post_url"):
                    result["post_url"] = info["post_url"]
                    result["post_id"] = info.get("post_id", "")
                    result["status"] = "published"
                    result["message"] = "Posted to TikTok"
                elif info.get("status"):
                    result["publish_status"] = info["status"]
            except Exception:
                pass  # non-fatal — the post still went through, URL just unresolved

        return result

    except Exception as e:
        return {"status": "error", "message": str(e)}
