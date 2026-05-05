"""Global API usage tracker — writes to api_usage_global.json."""
import os, json, threading
from datetime import datetime

_USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "api_usage_global.json")
_lock = threading.Lock()

# Cost rates per unit
COST_RATES = {
    "openai_tokens":          0.00000015,   # $0.15 / 1M tokens (gpt-4o-mini input)
    "anthropic_tokens":       0.00000025,   # $0.25 / 1M tokens (haiku input)
    "runway_seconds":         0.05,          # $0.05 / second
    "elevenlabs_chars":       0.0003,        # $0.30 / 1000 chars
    "replicate_predictions":  0.004,         # $0.004 / prediction
    "sendgrid_emails":        0.0,           # free tier
}

def _load() -> dict:
    if os.path.exists(_USAGE_FILE):
        try:
            with open(_USAGE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"entries": []}


def _save(data: dict):
    try:
        with open(_USAGE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def track(service: str, metric: str, quantity: float, user_id: str = "", feature: str = ""):
    """Record an API call. service=openai|anthropic|runway|elevenlabs|replicate|sendgrid
       metric=tokens|seconds|chars|predictions|emails  quantity=amount used"""
    with _lock:
        data = _load()
        data["entries"].append({
            "ts": datetime.utcnow().isoformat(),
            "service": service,
            "metric": metric,
            "quantity": quantity,
            "user_id": user_id,
            "feature": feature,
        })
        # Keep last 10000 entries (~30 days on heavy usage)
        data["entries"] = data["entries"][-10000:]
        _save(data)


def get_stats(month: str = None) -> dict:
    """Aggregate usage. month='YYYY-MM' or None for current month."""
    if month is None:
        month = datetime.utcnow().strftime("%Y-%m")
    today = datetime.utcnow().strftime("%Y-%m-%d")

    data = _load()
    entries = data.get("entries", [])

    month_entries = [e for e in entries if e.get("ts", "").startswith(month)]
    today_entries = [e for e in entries if e.get("ts", "").startswith(today)]

    def agg(ents):
        totals = {}
        costs = {}
        by_feature = {}
        users = set()
        for e in ents:
            svc = e.get("service", "unknown")
            qty = e.get("quantity", 0)
            key = f"{svc}_{e.get('metric', 'calls')}"
            totals[key] = totals.get(key, 0) + qty
            cost = qty * COST_RATES.get(key, 0)
            costs[svc] = costs.get(svc, 0) + cost
            feat = e.get("feature", "unknown")
            by_feature[feat] = by_feature.get(feat, 0) + cost
            if e.get("user_id"):
                users.add(e["user_id"])
        total_cost = sum(costs.values())
        most_expensive = max(by_feature, key=by_feature.get) if by_feature else None
        return {
            "totals": totals,
            "costs_by_service": costs,
            "total_cost": round(total_cost, 4),
            "by_feature": {k: round(v, 4) for k, v in by_feature.items()},
            "most_expensive_feature": most_expensive,
            "unique_users": len(users),
            "cost_per_user": round(total_cost / max(len(users), 1), 4),
        }

    month_stats = agg(month_entries)
    today_stats = agg(today_entries)
    return {
        "month": month,
        "today": today,
        "this_month": month_stats,
        "today": today_stats,
        "services": {
            "openai": {
                "name": "OpenAI",
                "icon": "🤖",
                "month_tokens": month_stats["totals"].get("openai_tokens", 0),
                "today_tokens": today_stats["totals"].get("openai_tokens", 0),
                "month_cost": round(month_stats["costs_by_service"].get("openai", 0), 4),
                "today_cost": round(today_stats["costs_by_service"].get("openai", 0), 4),
                "rate": "$0.15/1M tokens",
            },
            "anthropic": {
                "name": "Anthropic",
                "icon": "🧠",
                "month_tokens": month_stats["totals"].get("anthropic_tokens", 0),
                "today_tokens": today_stats["totals"].get("anthropic_tokens", 0),
                "month_cost": round(month_stats["costs_by_service"].get("anthropic", 0), 4),
                "today_cost": round(today_stats["costs_by_service"].get("anthropic", 0), 4),
                "rate": "$0.25/1M tokens",
            },
            "runway": {
                "name": "Runway",
                "icon": "🎬",
                "month_seconds": month_stats["totals"].get("runway_seconds", 0),
                "today_seconds": today_stats["totals"].get("runway_seconds", 0),
                "month_cost": round(month_stats["costs_by_service"].get("runway", 0), 4),
                "today_cost": round(today_stats["costs_by_service"].get("runway", 0), 4),
                "rate": "$0.05/second",
            },
            "elevenlabs": {
                "name": "ElevenLabs",
                "icon": "🎙️",
                "month_chars": month_stats["totals"].get("elevenlabs_chars", 0),
                "today_chars": today_stats["totals"].get("elevenlabs_chars", 0),
                "month_cost": round(month_stats["costs_by_service"].get("elevenlabs", 0), 4),
                "today_cost": round(today_stats["costs_by_service"].get("elevenlabs", 0), 4),
                "rate": "$0.30/1000 chars",
            },
            "replicate": {
                "name": "Replicate",
                "icon": "⚡",
                "month_predictions": month_stats["totals"].get("replicate_predictions", 0),
                "today_predictions": today_stats["totals"].get("replicate_predictions", 0),
                "month_cost": round(month_stats["costs_by_service"].get("replicate", 0), 4),
                "today_cost": round(today_stats["costs_by_service"].get("replicate", 0), 4),
                "rate": "$0.004/prediction",
            },
            "sendgrid": {
                "name": "SendGrid",
                "icon": "📧",
                "month_emails": month_stats["totals"].get("sendgrid_emails", 0),
                "today_emails": today_stats["totals"].get("sendgrid_emails", 0),
                "month_cost": 0,
                "today_cost": 0,
                "rate": "Free tier",
            },
        },
        "summary": {
            "total_cost_today": today_stats["total_cost"],
            "total_cost_month": month_stats["total_cost"],
            "cost_per_user_month": month_stats["cost_per_user"],
            "most_expensive_feature": month_stats["most_expensive_feature"],
            "unique_users_month": month_stats["unique_users"],
        },
    }
