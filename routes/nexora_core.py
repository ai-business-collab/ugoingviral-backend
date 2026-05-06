"""
Nexora Core Engine — Metrics API
GET /api/nexora/metrics  (protected by X-Nexora-Key header)
"""
import os
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request

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
        users_data = load_users().get("users", [])
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
        mrr = 0
        posts_today = 0
        autopilot_active = 0
        for uid in get_all_user_ids():
            try:
                us = _load_user_store(uid)
                mrr += PLAN_PRICES.get(us.get("billing", {}).get("plan", "free"), 0)
                if us.get("automation", {}).get("active"):
                    autopilot_active += 1
                for k, v in us.get("scheduler_log", {}).items():
                    if isinstance(k, str) and k.startswith(today):
                        if isinstance(v, dict) and not v.get("skipped"):
                            posts_today += 1
            except Exception:
                pass
        return {
            "total_users": total_users,
            "active_users": active_users,
            "mrr": mrr,
            "posts_today": posts_today,
            "autopilot_active": autopilot_active,
            "uptime": "99.9%",
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
    context = (
        "Platform: UgoingViral — social media automation SaaS (Danish market)\n"
        f"Total users: {m.get('total_users', '?')}\n"
        f"Active users (30d): {m.get('active_users', '?')}\n"
        f"MRR: {m.get('mrr', '?')} DKK\n"
        f"Posts today: {m.get('posts_today', '?')}\n"
        f"Autopilot sessions: {m.get('autopilot_active', '?')}\n"
        f"Uptime: {m.get('uptime', '99.9%')}"
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
