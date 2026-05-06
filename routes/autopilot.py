"""
UgoingViral — Auto Pilot Route
GET  /api/autopilot/status  → current state + next posts + activity log
POST /api/autopilot/toggle  → enable/disable auto pilot
"""
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store, add_log, _load_user_store, _save_user_store

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


# ==============================================================================
# FULL AUTO MODE
# ==============================================================================

@router.get("/api/autopilot/full_auto_status")
def full_auto_status(current_user: dict = Depends(get_current_user)):
    ustore = _load_user_store(current_user["id"])
    return {
        "full_auto_enabled": ustore.get("full_auto_enabled", False),
        "full_auto_mode":    ustore.get("full_auto_mode", "review"),
        "last_run":          ustore.get("full_auto_last_run"),
        "last_summary":      ustore.get("full_auto_last_summary"),
    }


@router.post("/api/autopilot/full_auto_toggle")
async def full_auto_toggle(req: Request, current_user: dict = Depends(get_current_user)):
    d      = await req.json()
    uid    = current_user["id"]
    ustore = _load_user_store(uid)
    plan   = ustore.get("billing", {}).get("plan", "free")

    if plan not in {"pro", "elite", "personal", "agency"}:
        return {"ok": False, "error": "pro_required",
                "message": "Full Auto Mode requires Pro plan or above."}

    credits = ustore.get("billing", {}).get("credits", 0)
    if credits < 200:
        return {"ok": False, "error": "insufficient_credits",
                "message": "Minimum 200 credits required.",
                "credits": credits, "minimum": 200}

    enabled = bool(d.get("enabled", False))
    mode    = str(d.get("mode", "review")).lower()
    if mode not in ("review", "autonomous"):
        mode = "review"

    ustore["full_auto_enabled"] = enabled
    ustore["full_auto_mode"]    = mode
    _save_user_store(uid, ustore)
    return {"ok": True, "full_auto_enabled": enabled, "full_auto_mode": mode}


async def run_full_auto_mode():
    # Every 6 h: analyse performance, generate + schedule content for Full Auto users
    import asyncio as _asyncio
    import httpx   as _httpx
    import os      as _os
    from datetime import datetime as _dt, timedelta as _td
    from services.store import (
        get_all_user_ids  as _uids,
        _load_user_store  as _lus,
        _save_user_store  as _sus,
        set_user_context  as _sctx,
        reset_user_context as _rctx,
    )
    from services.users import load_users as _lu

    _BOT_TOKEN = _os.getenv("TELEGRAM_BOT_TOKEN", "")
    _PRO_PLUS  = {"pro", "elite", "personal", "agency"}

    async def _send_tg(chat_id, text):
        if not _BOT_TOKEN or not chat_id:
            return
        try:
            async with _httpx.AsyncClient(timeout=8) as c:
                await c.post(
                    "https://api.telegram.org/bot" + _BOT_TOKEN + "/sendMessage",
                    json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                )
        except Exception:
            pass

    await _asyncio.sleep(180)

    while True:
        await _asyncio.sleep(6 * 3600)

        try:
            uids = _uids()
        except Exception:
            continue

        users_map = {}
        try:
            for u in _lu().get("users", []):
                if isinstance(u, dict) and u.get("id"):
                    users_map[u["id"]] = u
        except Exception:
            pass

        for uid in uids:
            try:
                ustore = _lus(uid)
                if not ustore.get("full_auto_enabled", False):
                    continue
                plan = ustore.get("billing", {}).get("plan", "free")
                if plan not in _PRO_PLUS:
                    continue
                credits = ustore.get("billing", {}).get("credits", 0)
                if credits < 200:
                    ustore["full_auto_enabled"] = False
                    _sus(uid, ustore)
                    tg = users_map.get(uid, {}).get("telegram_id") or ustore.get("telegram_chat_id")
                    await _send_tg(tg, "⚠️ *UgoingViral — Full Auto paused*\n\nInsufficient credits (min 200). Top up to resume.")
                    continue

                insights = {}
                try:
                    from routes.agent import _get_content_insights
                    insights = _get_content_insights(uid) or {}
                except Exception:
                    pass

                auto      = ustore.get("automation", {})
                niche     = auto.get("niche", "") or ustore.get("niche", "general")
                platforms = [p for p, cfg in auto.get("platforms", {}).items() if cfg.get("active")]
                if not platforms:
                    platforms = ["instagram"]
                lang     = auto.get("language", "english")
                fa_mode  = ustore.get("full_auto_mode", "review")
                best_hr  = insights.get("best_posting_hour") or 9

                generated = []
                tokens = _sctx(uid)
                try:
                    from routes.scheduler import _generate_autopilot_content
                    for plat in platforms[:2]:
                        try:
                            res = await _generate_autopilot_content(niche=niche, platform=plat, language=lang)
                            if not res.get("caption"):
                                continue
                            tags    = " ".join(res.get("hashtags", []))
                            caption = res["caption"] + ("\n\n" + tags if tags else "")
                            sched   = (_dt.utcnow() + _td(days=1)).replace(
                                hour=best_hr, minute=0, second=0, microsecond=0)
                            post = {
                                "id":             "fa_" + _dt.utcnow().strftime("%Y%m%d%H%M%S") + "_" + uid[:6],
                                "caption":        caption,
                                "platform":       plat,
                                "scheduled_time": sched.isoformat(),
                                "status":  "pending_approval" if fa_mode == "review" else "scheduled",
                                "source":  "full_auto",
                                "created_at": _dt.utcnow().isoformat(),
                            }
                            ustore.setdefault("full_auto_posts", []).append(post)
                            generated.append({"platform": plat, "caption": caption[:80],
                                              "scheduled": sched.isoformat()[:16]})
                        except Exception:
                            continue
                finally:
                    _rctx(tokens)

                if not generated:
                    continue

                ustore["full_auto_posts"]    = ustore.get("full_auto_posts", [])[-50:]
                ustore["full_auto_last_run"] = _dt.utcnow().isoformat()

                action = "ready for your approval" if fa_mode == "review" else "scheduled automatically"
                lines  = ["\U0001f916 *UgoingViral — Full Auto Mode*\n"]
                for g in generated:
                    lines.append("✅ *" + g["platform"].capitalize() + "* — " + action)
                    lines.append("“" + g["caption"] + "...”")
                    lines.append("\U0001f4c5 " + g["scheduled"])
                if insights.get("best_platform"):
                    lines.append("\n\U0001f4ca Best platform: " + insights["best_platform"])
                summary = "\n".join(lines)
                ustore["full_auto_last_summary"] = summary[:600]
                _sus(uid, ustore)

                tg = users_map.get(uid, {}).get("telegram_id") or ustore.get("telegram_chat_id")
                await _send_tg(tg, summary)

            except Exception:
                continue
