"""
UgoingViral — Growth Dashboard
GET /api/growth/overview     — follower data, engagement, best times, top hashtags
GET /api/growth/platforms    — per-platform stats
"""
from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import APIRouter, Depends
from routes.auth import get_current_user
from services.store import store

router = APIRouter()

PLATFORM_ICONS = {
    "instagram": "IG", "tiktok": "TT", "facebook": "FB",
    "twitter": "TW", "youtube": "YT"
}

OPTIMAL_TIMES = {
    "instagram": ["08:00", "12:00", "17:00", "19:00"],
    "tiktok":    ["07:00", "15:00", "21:00"],
    "facebook":  ["09:00", "13:00", "16:00"],
    "twitter":   ["08:00", "12:00", "17:00", "22:00"],
    "youtube":   ["14:00", "20:00"],
}

CONTENT_TYPE_TIPS = {
    "reel":     "Reels get 3x more reach than static posts",
    "video":    "Video content averages 48% more engagement",
    "carousel": "Carousels drive 3x more swipe-throughs",
    "image":    "Single images work best for brand awareness",
    "story":    "Stories get direct replies from engaged followers",
}


@router.get("/api/growth/overview")
def get_growth_overview(current_user: dict = Depends(get_current_user)):
    now = datetime.now()
    days_back = 30

    # Posts per day (last 30 days)
    posts = store.get("scheduled_posts", [])
    posts_by_day = defaultdict(int)
    posts_by_platform = defaultdict(int)
    posts_by_type = defaultdict(int)
    posted_count = 0
    for p in posts:
        plat = p.get("platform", "unknown")
        ts_str = p.get("scheduled_time", p.get("posted_at", ""))
        try:
            ts = datetime.fromisoformat(ts_str)
            if (now - ts).days <= days_back:
                day = ts.strftime("%Y-%m-%d")
                posts_by_day[day] += 1
                posts_by_platform[plat] += 1
        except Exception:
            pass
        if p.get("posted"):
            posted_count += 1
            ctype = p.get("media_type", p.get("content_type", "image"))
            posts_by_type[ctype] += 1

    # Credit usage (activity proxy for engagement effort)
    usage = store.get("api_usage", [])
    credits_by_day = defaultdict(int)
    for entry in usage:
        try:
            ts = datetime.fromisoformat(entry["ts"])
            if (now - ts).days <= days_back:
                credits_by_day[ts.strftime("%Y-%m-%d")] += entry.get("credits", 0)
        except Exception:
            pass

    # Build 30-day arrays
    day_labels = []
    posts_series = []
    credits_series = []
    for i in range(29, -1, -1):
        day = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        day_labels.append((now - timedelta(days=i)).strftime("%b %d"))
        posts_series.append(posts_by_day.get(day, 0))
        credits_series.append(credits_by_day.get(day, 0))

    # Hashtag extraction from content history
    history = store.get("content_history", [])
    hashtag_counts = defaultdict(int)
    for h in history:
        text = h.get("content", h.get("caption", h.get("text", "")))
        if isinstance(text, str):
            import re
            tags = re.findall(r"#(\w+)", text)
            for tag in tags:
                hashtag_counts[tag.lower()] += 1

    top_hashtags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)[:15]

    # Content type performance
    content_types_ranked = sorted(
        [{"type": k, "count": v, "tip": CONTENT_TYPE_TIPS.get(k, "")} for k, v in posts_by_type.items()],
        key=lambda x: x["count"], reverse=True
    )

    # Recommended posting times (from actual post times if data exists, else defaults)
    post_hour_counts = defaultdict(int)
    for p in posts:
        if p.get("posted"):
            ts_str = p.get("posted_at", p.get("scheduled_time", ""))
            try:
                ts = datetime.fromisoformat(ts_str)
                post_hour_counts[ts.hour] += 1
            except Exception:
                pass

    best_hours = sorted(post_hour_counts.items(), key=lambda x: x[1], reverse=True)[:3]
    recommended_times = [f"{h:02d}:00" for h, _ in best_hours] if best_hours else ["09:00", "13:00", "18:00"]

    # Platform breakdown
    platform_breakdown = []
    active_platforms = set(p.get("platform") for p in posts if p.get("platform"))
    for plat in ["instagram", "tiktok", "facebook", "twitter", "youtube"]:
        count = posts_by_platform.get(plat, 0)
        connected = bool(store.get("settings", {}).get(f"{plat}_api_connected") or
                        store.get("settings", {}).get(f"{plat}_user"))
        if count > 0 or connected:
            platform_breakdown.append({
                "platform": plat,
                "posts": count,
                "connected": connected,
                "optimal_times": OPTIMAL_TIMES.get(plat, ["09:00", "14:00", "19:00"]),
            })

    # Engagement rate estimate (posts posted / total posts scheduled * 100)
    total_scheduled = len(posts)
    engagement_rate = round(100 * posted_count / total_scheduled, 1) if total_scheduled > 0 else 0

    return {
        "day_labels": day_labels,
        "posts_series": posts_series,
        "credits_series": credits_series,
        "total_posted": posted_count,
        "total_scheduled": total_scheduled,
        "engagement_rate": engagement_rate,
        "platform_breakdown": platform_breakdown,
        "content_types": content_types_ranked,
        "top_hashtags": [{"tag": t, "count": c} for t, c in top_hashtags],
        "recommended_times": recommended_times,
        "posts_by_platform": dict(posts_by_platform),
        "period_days": days_back,
    }


@router.get("/api/growth/platforms")
def get_platform_growth(current_user: dict = Depends(get_current_user)):
    settings = store.get("settings", {})
    platforms = {}
    for p in ["instagram", "tiktok", "facebook", "twitter", "youtube"]:
        connected = bool(settings.get(f"{p}_api_connected") or settings.get(f"{p}_user"))
        username = settings.get(f"{p}_username") or settings.get(f"{p}_user", "")
        platforms[p] = {
            "connected": connected,
            "username": username,
            "optimal_times": OPTIMAL_TIMES.get(p, []),
        }
    return {"platforms": platforms}
