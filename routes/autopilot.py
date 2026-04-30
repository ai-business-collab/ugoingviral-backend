"""
UgoingViral — Auto Pilot Route
GET  /api/autopilot/status  → current state + next posts + activity log
POST /api/autopilot/toggle  → enable/disable auto pilot
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store, add_log

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
                "caption":   (sp.get("caption","") or "")[:80] + ("…" if len(sp.get("caption","")) > 80 else ""),
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
    }


@router.post("/api/autopilot/toggle")
async def toggle_autopilot(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    active = bool(d.get("active", False))
    if "automation" not in store:
        store["automation"] = {}
    store["automation"]["active"] = active
    save_store()
    status = "enabled" if active else "disabled"
    add_log(f"🤖 Auto Pilot {status}", "info")
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
    save_store()
    return {"ok": True}
