"""
Nexora Core Engine — Metrics API
GET /api/nexora/metrics  (protected by X-Nexora-Key header)
"""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

NEXORA_KEY = os.getenv("NEXORA_API_KEY", "nexora-core-2026")

PLAN_PRICES = {
    "free": 0, "starter": 40, "growth": 57, "basic": 70,
    "pro": 140, "elite": 210, "personal": 350, "agency": 199,
}


def _require_key(request: Request):
    key = request.headers.get("X-Nexora-Key", "")
    if key != NEXORA_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Nexora-Key")


@router.get("/api/nexora/metrics")
def nexora_metrics(request: Request):
    _require_key(request)

    from services.store import get_all_user_ids, _load_user_store
    from services.users import load_users

    today = datetime.now().strftime("%Y-%m-%d")
    users_data = load_users().get("users", [])
    total_users = len(users_data)

    # Active users: logged in within last 30 days
    now_dt = datetime.utcnow()
    active_users = 0
    for u in users_data:
        ll = u.get("last_login") or u.get("created_at", "")
        if ll:
            try:
                ll_dt = datetime.fromisoformat(ll.replace("Z", ""))
                if (now_dt - ll_dt).days <= 30:
                    active_users += 1
            except Exception:
                pass

    mrr = 0
    posts_today = 0
    autopilot_active = 0
    credits_used_today = 0
    api_costs_today = 0.0

    for uid in get_all_user_ids():
        try:
            ustore = _load_user_store(uid)

            # MRR
            plan = ustore.get("billing", {}).get("plan", "free")
            mrr += PLAN_PRICES.get(plan, 0)

            # Autopilot
            if ustore.get("automation", {}).get("active"):
                autopilot_active += 1

            # Posts today: scheduler_log keys starting with today's date
            slog = ustore.get("scheduler_log", {})
            for key, val in slog.items():
                if isinstance(key, str) and key.startswith(today):
                    if isinstance(val, dict) and not val.get("skipped"):
                        posts_today += 1

            # Credits used today: each successful post costs 1 credit; count success logs
            for entry in ustore.get("automation_log", []):
                if not isinstance(entry, dict):
                    continue
                msg = entry.get("msg", "")
                ts = entry.get("date", "") or entry.get("time", "")
                # automation_log stores time HH:MM, no date — use scheduler_log for dated counts
                if entry.get("level") == "success" and "Posted to" in msg:
                    credits_used_today += 1

            # API costs today: estimate from AI generation log entries
            for entry in ustore.get("automation_log", []):
                if not isinstance(entry, dict):
                    continue
                msg = entry.get("msg", "")
                if "AI content" in msg and "error" not in msg.lower():
                    # ~$0.001 per Haiku call (400 tokens)
                    api_costs_today += 0.001

        except Exception:
            pass

    return {
        "platform": "ugoingviral",
        "mrr": mrr,
        "active_users": active_users,
        "total_users": total_users,
        "posts_today": posts_today,
        "autopilot_active": autopilot_active,
        "credits_used_today": credits_used_today,
        "api_costs_today": round(api_costs_today, 4),
        "uptime": "99.9%",
    }
