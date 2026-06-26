"""
Nexora Core Engine — Metrics API
GET /api/nexora/metrics  (protected by X-Nexora-Key header)
"""
import os
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request

router = APIRouter()

NEXORA_KEY = os.getenv("NEXORA_API_KEY", "nexora-core-2026")

# Plan list prices (DKK/mo). Used ONLY to show how many users sit on each tier —
# NEVER as a revenue figure. Real revenue comes from Stripe (services.real_revenue).
PLAN_PRICES = {
    "free": 0, "starter": 40, "growth": 57, "basic": 70,
    "pro": 140, "elite": 210, "personal": 350, "agency": 199,
}

# Process start — real uptime is measured from here (the moment this module is
# first imported, i.e. app boot). No fabricated availability percentage.
_PROCESS_START = time.time()


def _is_test_user(email: str) -> bool:
    """QA/automation accounts (qa-…@ugoingviral.com) are not real users and must
    not inflate the user counts Nex reports to the owner."""
    e = (email or "").lower()
    local = e.split("@", 1)[0]
    return local.startswith("qa-") or local in ("qa", "qa-agent", "qa-auto")


def _real_users(users_data: list) -> list:
    return [u for u in users_data if not _is_test_user(u.get("email", ""))]


def _real_uptime() -> str:
    """Return a real, human-readable process uptime (e.g. '3d 4h 12m')."""
    secs = max(0, int(time.time() - _PROCESS_START))
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


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
    users_data = _real_users(load_users().get("users", []))
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

    posts_today = 0
    autopilot_active = 0
    credits_used_today = 0
    api_costs_today = 0.0

    for uid in get_all_user_ids():
        try:
            ustore = _load_user_store(uid)

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

    # Real per-service API costs from the global usage tracker (OpenAI,
    # ElevenLabs, Runway, Replicate, Anthropic). This is the canonical cost
    # source used by the admin dashboard; Nex's /costs command reads it live.
    costs = {}
    try:
        from services import api_tracker
        st   = api_tracker.get_stats()
        svc  = st.get("services", {})
        summ = st.get("summary", {})
        costs = {
            "openai_today":     svc.get("openai", {}).get("today_cost", 0),
            "openai_month":     svc.get("openai", {}).get("month_cost", 0),
            "elevenlabs_today": svc.get("elevenlabs", {}).get("today_cost", 0),
            "elevenlabs_month": svc.get("elevenlabs", {}).get("month_cost", 0),
            "runway_today":     svc.get("runway", {}).get("today_cost", 0),
            "runway_month":     svc.get("runway", {}).get("month_cost", 0),
            "runway_seconds_today": svc.get("runway", {}).get("today_seconds", 0),
            "runway_seconds_month": svc.get("runway", {}).get("month_seconds", 0),
            "replicate_today":  svc.get("replicate", {}).get("today_cost", 0),
            "replicate_month":  svc.get("replicate", {}).get("month_cost", 0),
            "anthropic_today":  svc.get("anthropic", {}).get("today_cost", 0),
            "anthropic_month":  svc.get("anthropic", {}).get("month_cost", 0),
            "total_today":      summ.get("total_cost_today", 0),
            "total_month":      summ.get("total_cost_month", 0),
        }
    except Exception:
        costs = {}
    # Fallback: if the tracker has no monthly data yet, extrapolate the rough
    # automation-log estimate so /costs still returns a real number.
    if not costs.get("total_month"):
        costs.setdefault("total_today", round(api_costs_today, 4))
        costs["total_month"] = round(api_costs_today * 30, 4)
        costs["estimated"] = True

    # Real revenue — Stripe active subscriptions only (never plan assignments).
    from services.real_revenue import real_mrr
    rev = real_mrr()

    return {
        "platform": "ugoingviral",
        "mrr": rev["mrr"],
        "mrr_currency": rev.get("currency"),
        "paying_subscriptions": rev.get("active_subscriptions", 0),
        "revenue_source": rev.get("source", "stripe"),
        "stripe_configured": rev.get("stripe_configured", False),
        "active_users": active_users,
        "total_users": total_users,
        "posts_today": posts_today,
        "autopilot_active": autopilot_active,
        "credits_used_today": credits_used_today,
        "api_costs_today": round(api_costs_today, 4),
        "costs": costs,
        "uptime": _real_uptime(),
    }


# ── NEX CHAT ──────────────────────────────────────────────────────────────────
from fastapi.security import HTTPBearer as _NexBearer, HTTPAuthorizationCredentials as _NexCreds

_nex_sec = _NexBearer(auto_error=True)


def _require_owner_for_nex(creds: _NexCreds = Depends(_nex_sec)):
    try:
        from routes.admin import _jwt_secret, _decode_token
        info = _decode_token(creds.credentials)
        if not info or info.get("role") != "owner":
            raise HTTPException(status_code=403, detail="Owner access required")
        return info
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def _quick_metrics() -> dict:
    try:
        from services.store import get_all_user_ids, _load_user_store
        from services.users import load_users
        from datetime import datetime as _dt
        today = _dt.now().strftime("%Y-%m-%d")
        users_data = _real_users(load_users().get("users", []))
        total_users = len(users_data)
        now_dt = _dt.utcnow()
        active_users = 0
        for u in users_data:
            ll = u.get("last_login") or u.get("created_at", "")
            if ll:
                try:
                    if (_dt.utcnow() - _dt.fromisoformat(ll.replace("Z", ""))).days <= 30:
                        active_users += 1
                except Exception:
                    pass
        posts_today = 0
        autopilot_active = 0
        for uid in get_all_user_ids():
            try:
                us = _load_user_store(uid)
                if us.get("automation", {}).get("active"):
                    autopilot_active += 1
                for k, v in us.get("scheduler_log", {}).items():
                    if isinstance(k, str) and k.startswith(today):
                        if isinstance(v, dict) and not v.get("skipped"):
                            posts_today += 1
            except Exception:
                pass
        from services.real_revenue import real_mrr
        rev = real_mrr()
        return {
            "total_users": total_users,
            "active_users": active_users,
            "mrr": rev["mrr"],
            "mrr_currency": rev.get("currency"),
            "paying_subscriptions": rev.get("active_subscriptions", 0),
            "posts_today": posts_today,
            "autopilot_active": autopilot_active,
            "uptime": _real_uptime(),
        }
    except Exception:
        return {}


@router.post("/api/nex/chat")
async def nex_chat(req: Request, admin=Depends(_require_owner_for_nex)):
    body = await req.json()
    message = str(body.get("message", "")).strip()[:1000]
    if not message:
        return {"response": "Please ask a question."}

    m = _quick_metrics()
    cur = m.get("mrr_currency") or "DKK"
    context = (
        "Platform: UgoingViral — social media automation SaaS (Danish market)\n"
        f"Total users (real DB count): {m.get('total_users', 0)}\n"
        f"Active users (30d): {m.get('active_users', 0)}\n"
        f"MRR (real, from Stripe active subscriptions): {m.get('mrr', 0)} {cur}\n"
        f"Paying subscriptions (Stripe active): {m.get('paying_subscriptions', 0)}\n"
        f"Posts today: {m.get('posts_today', 0)}\n"
        f"Autopilot sessions: {m.get('autopilot_active', 0)}\n"
        f"Uptime (since last restart): {m.get('uptime', '?')}\n"
        "IMPORTANT: These are the ONLY real numbers. Never invent or estimate "
        "users, revenue, or growth figures beyond what is given here. If a number "
        "is 0, report it as 0."
    )

    openai_key = os.getenv("OPENAI_API_KEY", "")
    if not openai_key:
        return {"response": "OpenAI API key not configured. Please add OPENAI_API_KEY to .env."}

    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                json={
                    "model": "gpt-4o-mini",
                    "max_tokens": 500,
                    "messages": [
                        {"role": "system", "content": (
                            "You are Nex, the AI CEO assistant for NexCore Labs / UgoingViral. "
                            "You have access to all platform data. Answer questions about the business "
                            "concisely and accurately. Use the metrics provided.\n\n"
                            f"Current metrics:\n{context}"
                        )},
                        {"role": "user", "content": message},
                    ],
                },
            )
        data = r.json()
        response = data.get("choices", [{}])[0].get("message", {}).get("content", "No response.")
        return {"response": response}
    except Exception as e:
        return {"response": f"Error: {str(e)[:120]}"}
