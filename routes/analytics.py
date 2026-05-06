"""
Analytics Dashboard
GET  /api/analytics/dashboard  — internal post/content analytics
GET  /api/analytics/platform   — real Meta / YouTube / TikTok data
POST /api/analytics/update_performance
GET  /api/analytics/performance
"""
import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import httpx
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, _load_user_store, _save_user_store

router = APIRouter()

_LOGGER = logging.getLogger(__name__)

_PLATFORM_NAMES = {
    "instagram": "Instagram", "tiktok": "TikTok",
    "youtube": "YouTube", "twitter": "X / Twitter",
    "facebook": "Facebook", "linkedin": "LinkedIn",
}

_GRAPH_API = "https://graph.facebook.com/v19.0"
_YT_API    = "https://www.googleapis.com/youtube/v3"
_TT_API    = "https://open.tiktokapis.com/v2"


# ── existing dashboard endpoint ───────────────────────────────────────────────

@router.get("/api/analytics/dashboard")
def analytics_dashboard(current_user: dict = Depends(get_current_user)):
    now = datetime.now()
    days = 30
    day_set = set()
    day_labels = []
    for i in range(days - 1, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        day_labels.append(d)
        day_set.add(d)

    posts = store.get("scheduled_posts", [])
    posts_per_day: dict = defaultdict(int)
    platform_counts: dict = defaultdict(int)
    hour_counts: dict = defaultdict(int)
    best_post = None
    best_eng = -1

    for p in posts:
        ts_str = p.get("scheduled_time") or p.get("created_at") or ""
        platform = p.get("platform", "unknown")
        platform_counts[platform] += 1
        try:
            ts = datetime.fromisoformat(ts_str)
            day = ts.strftime("%Y-%m-%d")
            if day in day_set:
                posts_per_day[day] += 1
            hour_counts[ts.hour] += 1
        except Exception:
            pass
        eng = p.get("engagement") or p.get("likes") or p.get("views") or 0
        if eng > best_eng:
            best_eng = eng
            best_post = p

    if best_post is None and posts:
        best_post = posts[-1]

    history = store.get("history", [])
    content_30d = 0
    for h in history:
        ts_str = h.get("created_at") or h.get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_str)
            if (now - ts).days <= days:
                content_30d += 1
        except Exception:
            pass

    best_hour = max(hour_counts, key=lambda h: hour_counts[h]) if hour_counts else None
    top_platform_raw = max(platform_counts, key=lambda k: platform_counts[k]) if platform_counts else None
    top_platform = _PLATFORM_NAMES.get(top_platform_raw, top_platform_raw.title()) if top_platform_raw else "–"

    platform_breakdown = {
        _PLATFORM_NAMES.get(k, k.title()): v
        for k, v in sorted(platform_counts.items(), key=lambda x: x[1], reverse=True)
    }

    bp_data = None
    if best_post:
        bp_data = {
            "caption": (best_post.get("caption") or best_post.get("content") or "")[:200],
            "platform": _PLATFORM_NAMES.get(best_post.get("platform", ""), best_post.get("platform", "")),
            "date": (best_post.get("scheduled_time") or "")[:10],
            "engagement": best_eng if best_eng > 0 else None,
        }

    return {
        "kpis": {
            "total_posts_30d": sum(posts_per_day.values()),
            "top_platform": top_platform,
            "best_hour": f"{best_hour:02d}:00" if best_hour is not None else "–",
            "content_generated_30d": content_30d,
        },
        "posts_per_day": {d: posts_per_day.get(d, 0) for d in day_labels},
        "platform_breakdown": platform_breakdown,
        "hour_heatmap": {str(h): hour_counts.get(h, 0) for h in range(24)},
        "best_post": bp_data,
        "day_labels": day_labels,
    }


# ── performance tracking ──────────────────────────────────────────────────────

@router.post("/api/analytics/update_performance")
async def update_performance(request: Request, current_user: dict = Depends(get_current_user)):
    """Record or update engagement metrics for a posted piece of content."""
    body     = await request.json()
    post_id  = str(body.get("post_id", ""))
    platform = str(body.get("platform", "")).lower()
    views    = max(0, int(body.get("views",    0)))
    likes    = max(0, int(body.get("likes",    0)))
    comments = max(0, int(body.get("comments", 0)))
    shares   = max(0, int(body.get("shares",   0)))

    engagement_rate = round((likes + comments + shares) / views, 4) if views > 0 else 0.0

    uid    = current_user["id"]
    ustore = _load_user_store(uid)
    perf   = ustore.setdefault("content_performance", [])

    existing = next((p for p in perf if p.get("post_id") == post_id), None)
    if existing:
        existing.update({
            "views": views, "likes": likes, "comments": comments,
            "shares": shares, "engagement_rate": engagement_rate,
            "updated_at": datetime.now().isoformat(),
        })
    else:
        perf.append({
            "post_id":        post_id,
            "platform":       platform,
            "content_type":   str(body.get("content_type", "")),
            "caption":        str(body.get("caption", ""))[:300],
            "hashtags":       body.get("hashtags", []),
            "posted_at":      str(body.get("posted_at", datetime.now().isoformat())),
            "views":          views,
            "likes":          likes,
            "comments":       comments,
            "shares":         shares,
            "engagement_rate": engagement_rate,
        })

    ustore["content_performance"] = perf[-500:]
    _save_user_store(uid, ustore)
    return {"ok": True, "post_id": post_id, "engagement_rate": engagement_rate}


@router.get("/api/analytics/performance")
def get_performance(current_user: dict = Depends(get_current_user)):
    """Return all tracked performance data for the current user."""
    ustore = _load_user_store(current_user["id"])
    perf   = ustore.get("content_performance", [])
    if not perf:
        return {"performance": [], "summary": {}}

    sorted_p = sorted(perf, key=lambda x: x.get("engagement_rate", 0), reverse=True)
    avg_er   = round(sum(p.get("engagement_rate", 0) for p in perf) / len(perf), 4)
    return {
        "performance": sorted_p[:50],
        "summary": {
            "total_tracked":   len(perf),
            "avg_engagement":  avg_er,
            "best_post":       sorted_p[0] if sorted_p else None,
        },
    }


# ── real platform analytics ───────────────────────────────────────────────────

def fetch_instagram_analytics(user_id: str) -> dict:
    """Fetch Instagram insights + recent media via Graph API."""
    ustore   = _load_user_store(user_id)
    settings = ustore.get("settings", {})
    token    = settings.get("instagram_api_token")
    ig_id    = settings.get("instagram_ig_id")
    if not token or not ig_id:
        return {"connected": False}

    since = int((datetime.now(timezone.utc) - timedelta(days=8)).timestamp())
    until = int(datetime.now(timezone.utc).timestamp())

    try:
        with httpx.Client(timeout=15.0) as client:
            insight_resp = client.get(
                f"{_GRAPH_API}/{ig_id}/insights",
                params={
                    "metric": "impressions,reach,profile_views,follower_count",
                    "period": "day",
                    "since": since,
                    "until": until,
                    "access_token": token,
                },
            )
            insight_data = insight_resp.json()

            media_resp = client.get(
                f"{_GRAPH_API}/{ig_id}/media",
                params={
                    "fields": "id,caption,timestamp,like_count,comments_count,media_type,permalink",
                    "limit": 10,
                    "access_token": token,
                },
            )
            media_data = media_resp.json()

        metrics: dict = {}
        for entry in insight_data.get("data", []):
            name = entry.get("name")
            vals = entry.get("values", [])
            if vals:
                metrics[name] = [v.get("value", 0) for v in vals[-7:]]

        follower_series = metrics.get("follower_count", [])
        current_followers = follower_series[-1] if follower_series else 0
        follower_growth = (follower_series[-1] - follower_series[0]) if len(follower_series) >= 2 else 0

        posts = []
        for m in media_data.get("data", []):
            likes    = m.get("like_count", 0)
            comments = m.get("comments_count", 0)
            posts.append({
                "id":        m.get("id"),
                "caption":   (m.get("caption") or "")[:150],
                "timestamp": m.get("timestamp"),
                "likes":     likes,
                "comments":  comments,
                "type":      m.get("media_type"),
                "url":       m.get("permalink"),
                "engagement": likes + comments,
            })

        return {
            "connected":          True,
            "followers":          current_followers,
            "follower_growth_7d": follower_growth,
            "follower_series_7d": follower_series,
            "impressions_7d":     sum(metrics.get("impressions", [])),
            "reach_7d":           sum(metrics.get("reach", [])),
            "profile_views_7d":   sum(metrics.get("profile_views", [])),
            "top_posts":          sorted(posts, key=lambda x: x["engagement"], reverse=True)[:5],
            "fetched_at":         datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        _LOGGER.error("Instagram analytics error for %s: %s", user_id, exc)
        return {"connected": True, "error": str(exc)}


def _refresh_youtube_token(refresh_token: str) -> str | None:
    client_id     = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    if not client_id or not client_secret:
        return None
    try:
        resp = httpx.post(
            "https://oauth2.googleapis.com/token",
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "client_id":     client_id,
                "client_secret": client_secret,
            },
            timeout=10.0,
        )
        return resp.json().get("access_token")
    except Exception:
        return None


def fetch_youtube_analytics(user_id: str) -> dict:
    """Fetch YouTube channel stats + recent video performance."""
    ustore        = _load_user_store(user_id)
    settings      = ustore.get("settings", {})
    access_token  = settings.get("youtube_access_token")
    refresh_token = settings.get("youtube_refresh_token")
    if not access_token:
        return {"connected": False}

    try:
        with httpx.Client(timeout=15.0) as client:
            headers = {"Authorization": f"Bearer {access_token}"}

            ch_resp = client.get(
                f"{_YT_API}/channels",
                params={"part": "statistics,snippet", "mine": "true"},
                headers=headers,
            )
            if ch_resp.status_code == 401 and refresh_token:
                new_token = _refresh_youtube_token(refresh_token)
                if new_token:
                    headers = {"Authorization": f"Bearer {new_token}"}
                    ustore["settings"]["youtube_access_token"] = new_token
                    _save_user_store(user_id, ustore)
                    ch_resp = client.get(
                        f"{_YT_API}/channels",
                        params={"part": "statistics,snippet", "mine": "true"},
                        headers=headers,
                    )
            ch_data = ch_resp.json()

            search_resp = client.get(
                f"{_YT_API}/search",
                params={
                    "part":       "snippet",
                    "forMine":    "true",
                    "type":       "video",
                    "maxResults": 10,
                    "order":      "date",
                },
                headers=headers,
            )
            search_data = search_resp.json()
            video_ids = [
                i["id"]["videoId"]
                for i in search_data.get("items", [])
                if i.get("id", {}).get("videoId")
            ]

            videos = []
            if video_ids:
                vid_resp = client.get(
                    f"{_YT_API}/videos",
                    params={"part": "statistics,snippet", "id": ",".join(video_ids)},
                    headers=headers,
                )
                for v in vid_resp.json().get("items", []):
                    s      = v.get("statistics", {})
                    views  = int(s.get("viewCount",   0))
                    likes  = int(s.get("likeCount",   0))
                    cmts   = int(s.get("commentCount", 0))
                    videos.append({
                        "id":        v["id"],
                        "title":     v.get("snippet", {}).get("title", "")[:100],
                        "published": v.get("snippet", {}).get("publishedAt"),
                        "views":     views,
                        "likes":     likes,
                        "comments":  cmts,
                        "engagement": likes + cmts,
                    })

        items = ch_data.get("items", [])
        if not items:
            return {"connected": True, "error": "No channel found"}

        stats = items[0].get("statistics", {})
        return {
            "connected":   True,
            "subscribers": int(stats.get("subscriberCount", 0)),
            "total_views": int(stats.get("viewCount", 0)),
            "video_count": int(stats.get("videoCount", 0)),
            "top_videos":  sorted(videos, key=lambda x: x["views"], reverse=True)[:5],
            "fetched_at":  datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        _LOGGER.error("YouTube analytics error for %s: %s", user_id, exc)
        return {"connected": True, "error": str(exc)}


def fetch_tiktok_analytics(user_id: str) -> dict:
    """Fetch TikTok user info via Open API v2."""
    ustore   = _load_user_store(user_id)
    settings = ustore.get("settings", {})
    token    = settings.get("tiktok_access_token")
    if not token:
        return {"connected": False}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(
                f"{_TT_API}/user/info/",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type":  "application/json",
                },
                json={"fields": [
                    "follower_count", "following_count",
                    "likes_count", "video_count", "display_name",
                ]},
            )
            data = resp.json()

        info = data.get("data", {}).get("user", {})
        if not info and "error" in data:
            return {"connected": True, "error": data["error"].get("message", "TikTok API error")}

        return {
            "connected":    True,
            "followers":    info.get("follower_count",  0),
            "following":    info.get("following_count", 0),
            "total_likes":  info.get("likes_count",     0),
            "video_count":  info.get("video_count",     0),
            "display_name": info.get("display_name",    ""),
            "fetched_at":   datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        _LOGGER.error("TikTok analytics error for %s: %s", user_id, exc)
        return {"connected": True, "error": str(exc)}


# ── platform analytics endpoint ───────────────────────────────────────────────

@router.get("/api/analytics/platform")
async def platform_analytics(current_user: dict = Depends(get_current_user)):
    """Return real platform analytics; fetches fresh data if cache is >6 h old."""
    uid      = current_user["id"]
    ustore   = _load_user_store(uid)
    settings = ustore.get("settings", {})
    pa       = ustore.setdefault("platform_analytics", {})
    cutoff   = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()

    async def _get(platform: str, fetch_fn):
        cached     = pa.get(platform, {})
        fetched_at = cached.get("fetched_at", "")
        if not fetched_at or fetched_at < cutoff:
            fresh = await asyncio.to_thread(fetch_fn, uid)
            pa[platform] = fresh
            ustore["platform_analytics"] = pa
            _save_user_store(uid, ustore)
            return fresh
        return cached

    result: dict = {}
    if settings.get("instagram_api_connected"):
        result["instagram"] = await _get("instagram", fetch_instagram_analytics)
    else:
        result["instagram"] = {"connected": False}

    if settings.get("youtube_api_connected"):
        result["youtube"] = await _get("youtube", fetch_youtube_analytics)
    else:
        result["youtube"] = {"connected": False}

    if settings.get("tiktok_api_connected"):
        result["tiktok"] = await _get("tiktok", fetch_tiktok_analytics)
    else:
        result["tiktok"] = {"connected": False}

    return result


# ── background sync task ──────────────────────────────────────────────────────

async def run_analytics_sync():
    """Refresh platform analytics for all connected users every 6 hours."""
    await asyncio.sleep(60)  # let the app fully start first
    _base = os.path.dirname(os.path.abspath(__file__))
    users_path = os.path.join(_base, "..", "users.json")

    while True:
        try:
            with open(users_path) as f:
                all_users = json.load(f)
            for u in all_users:
                uid = u.get("id")
                if not uid:
                    continue
                try:
                    ustore   = _load_user_store(uid)
                    settings = ustore.get("settings", {})
                    pa       = ustore.setdefault("platform_analytics", {})
                    changed  = False

                    if settings.get("instagram_api_connected"):
                        pa["instagram"] = await asyncio.to_thread(fetch_instagram_analytics, uid)
                        changed = True
                    if settings.get("youtube_api_connected"):
                        pa["youtube"] = await asyncio.to_thread(fetch_youtube_analytics, uid)
                        changed = True
                    if settings.get("tiktok_api_connected"):
                        pa["tiktok"] = await asyncio.to_thread(fetch_tiktok_analytics, uid)
                        changed = True

                    if changed:
                        ustore["platform_analytics"] = pa
                        _save_user_store(uid, ustore)
                except Exception as exc:
                    _LOGGER.error("Analytics sync failed for user %s: %s", uid, exc)
        except Exception as exc:
            _LOGGER.error("Analytics sync loop error: %s", exc)

        await asyncio.sleep(6 * 3600)
