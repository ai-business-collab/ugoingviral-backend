"""
UgoingViral — Auto Pilot Route
GET  /api/autopilot/status  → current state + next posts + activity log
POST /api/autopilot/toggle  → enable/disable auto pilot
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store, add_log, _load_user_store

router = APIRouter()

@router.get("/api/autopilot/status")
def autopilot_status(current_user: dict = Depends(get_current_user)):
    auto     = store.get("automation", {})
    active   = auto.get("active", False)
    times    = auto.get("post_times", ["09:00","14:00","18:00"])
    days     = auto.get("schedule_days", [1,2,3,4,5])
    platforms = [p for p, cfg in auto.get("platforms", {}).items() if cfg.get("active")]

    # Build next scheduled posts from scheduled_posts list
    now = datetime.utcnow()
    scheduled = store.get("scheduled_posts", [])
    upcoming = []
    for sp in scheduled:
        if not sp.get("posted", False):
            upcoming.append({
                "id":        sp.get("id",""),
                "caption":   (sp.get("caption","") or "")[:80] + ("..." if len(sp.get("caption","")) > 80 else ""),
                "platform":  sp.get("platform",""),
                "scheduled": sp.get("scheduled_time",""),
            })
    upcoming = upcoming[:5]

    # Recent activity from automation log
    log = store.get("automation_log", [])
    recent_log = list(reversed(log[-20:])) if log else []

    # Platform toggles
    platform_status = {}
    for p, cfg in auto.get("platforms", {}).items():
        platform_status[p] = {
            "active":     cfg.get("active", False),
            "auto_post":  cfg.get("auto_post", False),
        }

    return {
        "active":          active,
        "platforms":       platform_status,
        "post_times":      times,
        "schedule_days":   days,
        "upcoming_posts":  upcoming,
        "recent_log":      recent_log,
        "posts_per_day":   auto.get("posts_per_day", 3),
        "niche":           auto.get("niche", ""),
    }


@router.post("/api/autopilot/toggle")
async def toggle_autopilot(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    active = bool(d.get("active", False))

    if active:
        uid = current_user["id"]
        ustore = _load_user_store(uid)
        billing = ustore.get("billing", {})
        credits = billing.get("credits", 0)
        if credits < 300:
            return {
                "ok": False, "active": False,
                "error": "insufficient_credits",
                "message": "Insufficient credits. Minimum 300 credits required to activate autopilot.",
                "credits": credits, "minimum": 300,
            }

    if "automation" not in store:
        store["automation"] = {}
    store.get("automation", {})["active"] = active
    save_store()
    status = "enabled" if active else "disabled"
    add_log(f"Auto Pilot {status}", "info")
    return {"ok": True, "active": active}


@router.post("/api/autopilot/settings")
async def update_autopilot_settings(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    auto = store.setdefault("automation", {})
    if "post_times" in d:
        auto["post_times"] = d["post_times"]
    if "posts_per_day" in d:
        auto["posts_per_day"] = max(1, min(10, int(d["posts_per_day"])))
    if "schedule_days" in d:
        auto["schedule_days"] = d["schedule_days"]
    if "platforms" in d:
        for p, val in d["platforms"].items():
            if p not in auto.setdefault("platforms", {}):
                auto["platforms"][p] = {}
            auto["platforms"][p].update(val)
    if "niche" in d:
        auto["niche"] = str(d["niche"]).strip()
    save_store()
    return {"ok": True}


@router.post("/api/autopilot/run_now")
async def autopilot_run_now(current_user: dict = Depends(get_current_user)):
    """Immediately trigger autopilot for the current user.
    Bypasses quiet hours, weekday, time-of-day, and rate-limit guards.
    """
    from routes.scheduler import _run_for_user
    try:
        await _run_for_user(force=True)
        return {"ok": True, "message": "Auto Pilot triggered successfully"}
    except Exception as e:
        return {"ok": False, "message": str(e)[:200]}


async def run_autopilot_credit_checker():
    """Hourly background task: pause autopilot and alert users whose credits fall below 50."""
    import asyncio
    import os
    import httpx
    from services.store import _load_user_store, _save_user_store
    from services.users import load_users

    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TG_API = "https://api.telegram.org/bot"

    async def _send_tg(chat_id, text: str):
        if not BOT_TOKEN or not chat_id:
            return
        try:
            async with httpx.AsyncClient() as c:
                await c.post(
                    f"{TG_API}{BOT_TOKEN}/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                    timeout=10,
                )
        except Exception:
            pass

    while True:
        await asyncio.sleep(3600)  # run every hour
        try:
            users_data = load_users()
            for u in users_data.get("users", []):
                uid = u.get("id")
                if not uid:
                    continue
                try:
                    ustore = _load_user_store(uid)
                    auto = ustore.get("automation", {})
                    if not auto.get("active", False):
                        continue
                    credits = ustore.get("billing", {}).get("credits", 0)
                    if credits < 50:
                        auto["active"] = False
                        ustore["automation"] = auto
                        _save_user_store(uid, ustore)
                        tg_id = u.get("telegram_id")
                        await _send_tg(
                            tg_id,
                            "⚠️ UgoingViral: Your autopilot has been paused. "
                            "Balance below 50 credits. Top up to resume.",
                        )
                except Exception:
                    pass
        except Exception:
            pass
