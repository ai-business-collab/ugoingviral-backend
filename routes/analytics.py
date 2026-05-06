"""
Analytics Dashboard
GET /api/analytics/dashboard — full analytics for authenticated user
"""
from datetime import datetime, timedelta
from collections import defaultdict
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, _load_user_store, _save_user_store

router = APIRouter()

_PLATFORM_NAMES = {
    "instagram": "Instagram", "tiktok": "TikTok",
    "youtube": "YouTube", "twitter": "X / Twitter",
    "facebook": "Facebook", "linkedin": "LinkedIn",
}


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
