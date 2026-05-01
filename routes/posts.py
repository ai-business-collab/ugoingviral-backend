from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime, timedelta
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ── Posts ─────────────────────────────────────────────────────────────────────
@router.post("/api/posts/schedule")
def schedule_post(post: PostRequest):
    item = {"id": datetime.now().isoformat(), "platform": post.platform, "content": post.content,
            "image_url": post.image_url, "scheduled_time": post.scheduled_time or datetime.now().isoformat(),
            "product_id": post.product_id, "creator_id": post.creator_id,
            "status": "scheduled", "mode": post.mode or "ui"}
    store["scheduled_posts"].insert(0, item)
    # Auto bonus: first post ever
    try:
        from routes.billing import _try_auto_bonus
        _try_auto_bonus("first_post")
    except Exception:
        pass
    save_store()
    return {"status": "scheduled", "id": item["id"]}

@router.get("/api/posts/scheduled")
def get_scheduled(): return {"posts": store["scheduled_posts"]}

@router.delete("/api/posts/all")
def delete_all_posts():
    count = len(store.get("scheduled_posts", []))
    store["scheduled_posts"] = []
    store["scheduler_log"] = {}
    save_store()
    return {"status": "deleted", "count": count}

@router.delete("/api/posts/day/{date_str}")
def delete_day(date_str: str):
    before = len(store["scheduled_posts"])
    store["scheduled_posts"] = [p for p in store["scheduled_posts"] if not p.get("scheduled_time","").startswith(date_str)]
    log = store.get("scheduler_log", {})
    keys_to_del = [k for k in log if k.startswith(date_str)]
    for k in keys_to_del: del log[k]
    save_store()
    return {"status": "deleted", "count": before - len(store["scheduled_posts"])}

@router.delete("/api/posts/range/{start}/{end}")
def delete_range(start: str, end: str):
    from datetime import datetime, timedelta
    try:
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
    except:
        return {"status": "error", "message": "Ugyldig dato"}
    before = len(store["scheduled_posts"])
    def in_range(p):
        try:
            t = datetime.fromisoformat(p.get("scheduled_time",""))
            return start_dt <= t <= end_dt
        except: return False
    store["scheduled_posts"] = [p for p in store["scheduled_posts"] if not in_range(p)]
    log = store.get("scheduler_log", {})
    cur = start_dt
    while cur <= end_dt:
        ds = cur.strftime("%Y-%m-%d")
        for k in [k for k in log if k.startswith(ds)]: del log[k]
        cur += timedelta(days=1)
    save_store()
    return {"status": "deleted", "count": before - len(store["scheduled_posts"])}

@router.patch("/api/posts/{pid}/reschedule")
async def reschedule_post(pid: str, request: Request):
    """Update a post's scheduled_time for calendar drag-to-reschedule."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    new_time = str(body.get("scheduled_time", "")).strip()
    if not new_time:
        raise HTTPException(status_code=422, detail="scheduled_time required")
    posts = store.get("scheduled_posts", [])
    for p in posts:
        if p.get("id") == pid:
            p["scheduled_time"] = new_time
            save_store()
            return {"ok": True, "post": p}
    raise HTTPException(status_code=404, detail="Post not found")


@router.delete("/api/posts/{pid}")
def delete_post(pid: str):
    store["scheduled_posts"] = [p for p in store["scheduled_posts"] if p["id"] != pid]
    save_store(); return {"status": "deleted"}


# ── Repost ─────────────────────────────────────────────────────────────────────

@router.get("/api/posts/repost/settings")
def get_repost_settings():
    return store.get("repost_settings", {
        "enabled": False, "interval_days": 7, "max_per_week": 3, "platforms": []
    })


@router.post("/api/posts/repost/settings")
async def save_repost_settings(req: Request):
    d = await req.json()
    store["repost_settings"] = {
        "enabled": bool(d.get("enabled", False)),
        "interval_days": max(1, int(d.get("interval_days", 7))),
        "max_per_week": max(1, int(d.get("max_per_week", 3))),
        "platforms": d.get("platforms", []),
    }
    save_store()
    return {"status": "saved", "settings": store["repost_settings"]}


@router.post("/api/posts/repost")
async def create_repost(req: Request):
    d = await req.json()
    content = d.get("content", "")
    platform = d.get("platform", "")
    image_url = d.get("image_url", "")
    scheduled_time = d.get("scheduled_time") or (datetime.now() + timedelta(days=1)).isoformat()

    if not content or not platform:
        raise HTTPException(status_code=400, detail="Mangler content eller platform")

    item = {
        "id": datetime.now().isoformat() + "_repost",
        "platform": platform,
        "content": content,
        "image_url": image_url,
        "scheduled_time": scheduled_time,
        "status": "scheduled",
        "mode": "repost",
    }
    store["scheduled_posts"].insert(0, item)
    save_store()
    add_log(f"♻️ Genopslag planlagt til {platform} kl. {scheduled_time[11:16]}", "info")
    return {"status": "scheduled", "id": item["id"]}


@router.get("/api/posts/repost/history")
def get_repost_history():
    history = store.get("history", [])
    suitable = [
        h for h in history
        if h.get("content") and len(h.get("content", "")) > 20
    ]
    return {"items": suitable[:50]}


# ── Unified API Publish ────────────────────────────────────────────────────────

@router.post("/api/posts/publish_via_api")
async def publish_via_api(req: Request):
    """
    Publicer til Instagram eller TikTok via officielle APIs.
    Falder automatisk tilbage til Playwright hvis API ikke er forbundet.
    Body: { platform, content, image_url, video_url, post_id (optional) }
    """
    d = await req.json()
    platform  = d.get("platform", "")
    content   = d.get("content", "")
    image_url = d.get("image_url")
    video_url = d.get("video_url")
    post_id   = d.get("post_id") or datetime.now().isoformat()
    s = store.get("settings", {})

    result = {"status": "error", "message": "Ukendt platform", "platform": platform}

    # ── Instagram ────────────────────────────────────────────────────────────
    if platform == "instagram":
        if not s.get("instagram_api_connected"):
            return {"status": "error", "message": "Instagram API ikke forbundet", "platform": platform}
        try:
            from instagram_api import post_to_instagram, refresh_token_if_needed
            token      = s.get("instagram_api_token", "")
            ig_id      = s.get("instagram_ig_id", "")
            expires_at = s.get("instagram_api_expires")

            if not token or not ig_id:
                return {"status": "error", "message": "Instagram token mangler — forbind igen", "platform": platform}

            token, new_exp = await refresh_token_if_needed(token, expires_at)
            if new_exp:
                s["instagram_api_token"]   = token
                s["instagram_api_expires"] = new_exp
                save_store()

            image_urls = image_url if isinstance(image_url, list) else None
            single_img = image_url if isinstance(image_url, str) else None
            media_type = "REELS" if video_url else "IMAGE"

            api_result = await post_to_instagram(
                ig_account_id=ig_id,
                access_token=token,
                caption=content,
                image_url=single_img,
                image_urls=image_urls,
                video_url=video_url,
                media_type=media_type,
            )
            result = {
                "status":    api_result.get("status", "error"),
                "platform":  platform,
                "post_id":   api_result.get("id"),
                "message":   api_result.get("message", ""),
                "provider":  "instagram_graph_api",
            }
            add_log(
                f"📸 Instagram API opslag: {result['status']} — {api_result.get('id', '')}",
                "success" if result["status"] == "published" else "error",
            )
        except Exception as e:
            result = {"status": "error", "message": str(e), "platform": platform}
            add_log(f"❌ Instagram API fejl: {e}", "error")

    # ── TikTok ───────────────────────────────────────────────────────────────
    elif platform == "tiktok":
        if not s.get("tiktok_api_connected"):
            return {"status": "error", "message": "TikTok API ikke forbundet", "platform": platform}
        try:
            from tiktok_api import refresh_token_if_needed as tt_refresh, publish_to_tiktok
            token   = s.get("tiktok_access_token", "")
            refresh = s.get("tiktok_refresh_token", "")
            exp     = s.get("tiktok_expires_at")

            if not token:
                return {"status": "error", "message": "TikTok token mangler — forbind igen", "platform": platform}

            new_token, new_refresh, new_exp = await tt_refresh(token, refresh, exp)
            if new_exp:
                s["tiktok_access_token"]  = new_token
                s["tiktok_refresh_token"] = new_refresh or refresh
                s["tiktok_expires_at"]    = new_exp
                save_store()
                token = new_token

            image_urls = image_url if isinstance(image_url, list) else None
            single_img = image_url if isinstance(image_url, str) else None

            api_result = await publish_to_tiktok(
                access_token=token,
                caption=content,
                video_url=video_url,
                image_url=single_img,
                image_urls=image_urls,
                privacy_level=d.get("privacy_level", "SELF_ONLY"),
            )
            result = {
                "status":     api_result.get("status", "error"),
                "platform":   platform,
                "publish_id": api_result.get("publish_id"),
                "message":    api_result.get("message", ""),
                "provider":   "tiktok_content_posting_api",
            }
            add_log(
                f"🎵 TikTok API opslag: {result['status']} — {api_result.get('publish_id', '')}",
                "success" if result["status"] in ("published", "processing") else "error",
            )
        except Exception as e:
            result = {"status": "error", "message": str(e), "platform": platform}
            add_log(f"❌ TikTok API fejl: {e}", "error")

    # ── YouTube ──────────────────────────────────────────────────────────────
    elif platform == "youtube":
        if not s.get("youtube_api_connected"):
            return {"status": "error", "message": "YouTube API ikke forbundet", "platform": platform}
        try:
            from youtube_api import refresh_token_if_needed as yt_refresh, publish_to_youtube
            token   = s.get("youtube_access_token", "")
            refresh = s.get("youtube_refresh_token", "")
            exp     = s.get("youtube_expires_at")

            if not token:
                return {"status": "error", "message": "YouTube token mangler — forbind igen", "platform": platform}
            if not video_url:
                return {"status": "error", "message": "YouTube kræver en video URL", "platform": platform}

            new_token, new_exp = await yt_refresh(token, refresh, exp)
            if new_exp:
                s["youtube_access_token"] = new_token
                s["youtube_expires_at"]   = new_exp
                save_store()
                token = new_token

            is_short = bool(d.get("is_short", False))
            title    = d.get("title", content[:100]) if content else "Nyt opslag"

            api_result = await publish_to_youtube(
                access_token=token,
                title=title,
                description=content,
                video_url=video_url,
                tags=d.get("tags", []),
                privacy_status=d.get("privacy_status", "public"),
                is_short=is_short,
            )
            result = {
                "status":   api_result.get("status", "error"),
                "platform": platform,
                "video_id": api_result.get("video_id"),
                "url":      api_result.get("url"),
                "message":  api_result.get("message", ""),
                "provider": "youtube_data_api_v3",
            }
            add_log(
                f"▶️ YouTube API opslag: {result['status']} — {api_result.get('video_id', '')}",
                "success" if result["status"] == "published" else "error",
            )
        except Exception as e:
            result = {"status": "error", "message": str(e), "platform": platform}
            add_log(f"❌ YouTube API fejl: {e}", "error")

    # ── Gem i post historik ──────────────────────────────────────────────────
    history_entry = {
        "id":            post_id,
        "platform":      platform,
        "content":       content,
        "image_url":     image_url,
        "video_url":     video_url,
        "status":        result.get("status", "error"),
        "provider":      result.get("provider", "api"),
        "ig_post_id":    result.get("post_id"),
        "tt_publish_id": result.get("publish_id"),
        "yt_video_id":   result.get("video_id"),
        "yt_url":        result.get("url"),
        "message":       result.get("message", ""),
        "posted_at":     datetime.now().isoformat(),
        "type":          "api_post",
    }
    history = store.setdefault("post_history", [])
    history.insert(0, history_entry)
    store["post_history"] = history[:200]

    # Opdater scheduled post status hvis post_id matcher
    for sp in store.get("scheduled_posts", []):
        if sp.get("id") == post_id:
            sp["status"] = result.get("status", "error")
            sp["api_result"] = result
            break

    save_store()
    return result


@router.get("/api/posts/history")
def get_post_history():
    """Historik over API-postede opslag"""
    return {"history": store.get("post_history", [])[:50]}
