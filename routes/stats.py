from fastapi import APIRouter, Depends
from datetime import datetime, timedelta
from collections import defaultdict
from routes.auth import get_current_user
from services.store import store

router = APIRouter()


@router.get("/api/stats/overview")
def get_stats_overview(current_user: dict = Depends(get_current_user)):
    now = datetime.now()
    days_back = 30

    # Credit usage per day (last 30 days)
    usage_log = store.get("api_usage", [])
    credits_per_day = defaultdict(int)
    actions_per_type = defaultdict(int)
    for entry in usage_log:
        try:
            ts = datetime.fromisoformat(entry["ts"])
            if (now - ts).days <= days_back:
                day = ts.strftime("%Y-%m-%d")
                credits_per_day[day] += entry.get("credits", 0)
                actions_per_type[entry.get("action", "unknown")] += 1
        except Exception:
            pass

    # Posts per platform
    posts = store.get("scheduled_posts", [])
    posts_per_platform = defaultdict(int)
    posts_per_day = defaultdict(int)
    posts_this_month = 0
    for p in posts:
        plat = p.get("platform", "unknown")
        posts_per_platform[plat] += 1
        try:
            ts = datetime.fromisoformat(p.get("scheduled_time", ""))
            if (now - ts).days <= days_back:
                posts_per_day[ts.strftime("%Y-%m-%d")] += 1
                posts_this_month += 1
        except Exception:
            pass

    # Content history
    history = store.get("history", [])
    content_this_month = sum(
        1 for h in history
        if _within_days(h.get("created_at", h.get("ts", "")), days_back)
    )
    content_per_type = defaultdict(int)
    for h in history:
        content_per_type[h.get("type", h.get("content_type", "unknown"))] += 1

    # Pipeline runs
    pipeline_data = store.get("content_pipeline", {})
    total_videos = sum(
        len(v.get("videos", []))
        for v in pipeline_data.values()
        if isinstance(v, dict)
    )

    # Billing
    billing = store.get("billing", {})
    from routes.billing import PLANS
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    credits_left = billing.get("credits", plan_info["credits"])
    total_credits_used = sum(e.get("credits", 0) for e in usage_log)

    # Build day labels for last 14 days
    day_labels = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]

    return {
        "summary": {
            "posts_this_month":   posts_this_month,
            "content_this_month": content_this_month,
            "total_videos":       total_videos,
            "credits_left":       credits_left,
            "credits_used_total": total_credits_used,
            "plan":               plan_key,
        },
        "credits_per_day": {d: credits_per_day.get(d, 0) for d in day_labels},
        "posts_per_day":   {d: posts_per_day.get(d, 0) for d in day_labels},
        "posts_per_platform":  dict(posts_per_platform),
        "actions_per_type":    dict(actions_per_type),
        "content_per_type":    dict(content_per_type),
        "day_labels":          day_labels,
    }


def _within_days(ts_str: str, days: int) -> bool:
    try:
        ts = datetime.fromisoformat(ts_str)
        return (datetime.now() - ts).days <= days
    except Exception:
        return False


@router.get("/api/stats/providers")
def get_provider_stats(current_user: dict = Depends(get_current_user)):
    """Per-provider API usage breakdown for the cost dashboard."""
    import os
    now = datetime.now()
    log = store.get("provider_api_log", [])

    PROVIDER_META = {
        "openai":     {"name": "OpenAI",        "icon": "🧠", "color": "#10b981", "role": "Agent-hjerne + Whisper"},
        "anthropic":  {"name": "Anthropic",     "icon": "🔮", "color": "#7c3aed", "role": "Tung content & scripts"},
        "elevenlabs": {"name": "ElevenLabs",    "icon": "🎙️", "color": "#f59e0b", "role": "Voice-over & TTS"},
        "runway":     {"name": "Runway",        "icon": "🚀", "color": "#00e5ff", "role": "Video generation"},
        "luma":       {"name": "Luma",          "icon": "✨", "color": "#ec4899", "role": "Video generation"},
        "replicate":  {"name": "Replicate",     "icon": "⚡", "color": "#3b82f6", "role": "Video generation"},
    }

    # Aggregate by provider
    by_provider = defaultdict(lambda: {
        "calls": 0, "tokens_in": 0, "tokens_out": 0,
        "cost_usd": 0.0, "actions": defaultdict(int),
        "calls_today": 0, "calls_7d": 0,
    })

    for entry in log:
        prov = entry.get("provider", "unknown")
        ts_str = entry.get("ts", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            days_ago = (now - ts).days
        except Exception:
            days_ago = 999

        p = by_provider[prov]
        p["calls"] += 1
        p["tokens_in"] += entry.get("tokens_in", 0)
        p["tokens_out"] += entry.get("tokens_out", 0)
        p["cost_usd"] = round(p["cost_usd"] + entry.get("cost_usd", 0), 4)
        p["actions"][entry.get("action", "unknown")] += 1
        if days_ago == 0:
            p["calls_today"] += 1
        if days_ago <= 7:
            p["calls_7d"] += 1

    # Check which API keys are configured
    configured = {
        "openai":     bool(os.getenv("OPENAI_API_KEY")),
        "anthropic":  bool(os.getenv("ANTHROPIC_API_KEY")),
        "elevenlabs": bool(os.getenv("ELEVENLABS_API_KEY")),
        "runway":     bool(os.getenv("RUNWAY_API_KEY")),
        "luma":       bool(os.getenv("LUMA_API_KEY")),
        "replicate":  bool(os.getenv("REPLICATE_API_KEY")),
    }

    result = {}
    for key, meta in PROVIDER_META.items():
        stats = by_provider.get(key, {
            "calls": 0, "tokens_in": 0, "tokens_out": 0,
            "cost_usd": 0.0, "actions": {}, "calls_today": 0, "calls_7d": 0,
        })
        result[key] = {
            **meta,
            "configured": configured.get(key, False),
            "calls_total": stats["calls"],
            "calls_today": stats.get("calls_today", 0),
            "calls_7d": stats.get("calls_7d", 0),
            "tokens_in": stats["tokens_in"],
            "tokens_out": stats["tokens_out"],
            "cost_usd": stats["cost_usd"],
            "cost_dkk": round(stats["cost_usd"] * 7.0, 2),
            "top_actions": dict(sorted(
                stats.get("actions", {}).items(),
                key=lambda x: x[1], reverse=True
            )[:3]),
        }

    total_cost_usd = sum(v["cost_usd"] for v in result.values())
    return {
        "providers": result,
        "total_cost_usd": round(total_cost_usd, 4),
        "total_cost_dkk": round(total_cost_usd * 7.0, 2),
        "log_entries": len(log),
    }
