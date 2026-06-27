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

    # Platform toggles — show EVERY connected platform (not only ones already in
    # automation.platforms), so the user can enable YouTube/X/Facebook too.
    settings = store.get("settings", {}) or {}
    conns = store.get("connections", {}) or {}
    connected = set()
    for p in ("instagram", "tiktok", "youtube", "twitter", "facebook"):
        if settings.get(f"{p}_api_connected") or settings.get(f"{p}_access_token") or settings.get(f"{p}_api_token"):
            connected.add(p)
        v = conns.get(p)
        if isinstance(v, dict) and (v.get("connected") or v.get("username") or v.get("access_token")):
            connected.add(p)
    plat_cfg = auto.get("platforms", {}) or {}
    platform_status = {}
    for p in sorted(connected | set(plat_cfg.keys())):
        cfg = plat_cfg.get(p, {}) if isinstance(plat_cfg, dict) else {}
        platform_status[p] = {
            "active":     cfg.get("active", False),
            "auto_post":  cfg.get("auto_post", False),
            "connected":  p in connected,
        }

    # Plain-language reasons autopilot may not be posting right now.
    why = []
    ct_ap = (store.get("content_team", {}) or {}).get("autopilot", {}) or {}
    enabled_plats = [p for p, s in platform_status.items() if s["auto_post"]]
    enabled_connected = [p for p in enabled_plats if p in connected]
    if ct_ap.get("enabled"):
        why.append("True Auto Pilot (Content Team) is active and drives posting — the legacy real-time poster is paused to avoid double-posting.")
    if not active:
        why.append("Auto Pilot is OFF — turn it on.")
    if not connected:
        why.append("No social platform connected — connect one under Connect.")
    if not enabled_plats:
        why.append("No platform enabled for auto-post — enable at least one platform above.")
    elif not enabled_connected:
        why.append("Enabled platform(s) aren't connected — connect them or enable a connected one.")
    if not times:
        why.append("No posting times set.")
    if not (store.get("manual_products") or store.get("shopify_products_cache") or auto.get("niche")):
        why.append("No products and no niche set — add products or set a niche so content can be generated.")
    if active and enabled_connected and times and not why:
        why.append(f"All set — posts fire at your scheduled times ({', '.join(times)}). Use 'Post one now' to publish immediately.")

    return {
        "active":          active,
        "platforms":       platform_status,
        "connected_platforms": sorted(connected),
        "why_not_posting": why,
        "post_times":      times,
        "schedule_days":   days,
        "upcoming_posts":  upcoming,
        "recent_log":      recent_log,
        "posts_per_day":   auto.get("posts_per_day", 3),
        "niche":           auto.get("niche", ""),
    }


@router.post("/api/autopilot/toggle")
async def toggle_autopilot(req: Request, current_user: dict = Depends(get_current_user)):
    """Auto Pilot page master switch → drives content_team.autopilot (the single
    autonomous engine). Auto-derives a goal from the user's niche if none is set,
    so the page works without a separate goal step. The legacy real-time
    'auto-post' (Schedule page, automation.active) is a SEPARATE mode and is left
    untouched here — and it stands down whenever this is on (no double-posting)."""
    d = await req.json()
    active = bool(d.get("active", False))

    if active:
        uid = current_user["id"]
        ustore = _load_user_store(uid)
        billing = ustore.get("billing", {})
        credits = billing.get("credits", 0)
        if credits < 200:
            return {
                "ok": False, "active": False,
                "error": "insufficient_credits",
                "message": "You need at least 200 credits to start Auto Pilot",
                "credits": credits, "minimum": 200,
                "needed": max(0, 200 - int(credits)),
            }

    ct = store.get("content_team", {}) or {}
    ap = ct.get("autopilot") or {}
    if active and not (ct.get("goal") or "").strip():
        # Derive a goal so enabling is one click (no separate goal step).
        auto = store.get("automation", {}) or {}
        niche = (auto.get("niche") or store.get("settings", {}).get("niche") or "").strip()
        ct["goal"] = f"grow my {niche} audience" if niche else "grow my audience and engagement"
        ct["goal_updated_at"] = datetime.utcnow().isoformat() + "Z"
    ap["enabled"] = active
    ap.setdefault("auto_approve", True)
    ap.setdefault("interval_days", 7)
    ct["autopilot"] = ap
    store["content_team"] = ct
    save_store()
    status = "enabled" if active else "disabled"
    add_log(f"Auto Pilot {status}", "info")
    return {"ok": True, "active": active, "goal": ct.get("goal", "")}


def _spread_times(n: int) -> list:
    """Evenly spread N posting times across the active window (08:00–21:00).
    This is the bridge that makes 'posts per day' a REAL, honored setting —
    the scheduler reads post_times, so we regenerate them from N."""
    n = max(1, min(10, int(n)))
    start, end = 8 * 60, 21 * 60   # minutes
    if n == 1:
        slots = [12 * 60]
    else:
        step = (end - start) / (n - 1)
        slots = [int(round(start + step * i)) for i in range(n)]
    return [f"{m // 60:02d}:{m % 60:02d}" for m in slots]


@router.post("/api/autopilot/settings")
async def update_autopilot_settings(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    auto = store.setdefault("automation", {})
    # ONE source of truth for cadence: post_times is what the scheduler honors.
    # posts_per_day and post_times are kept in sync so neither is silently ignored.
    if "post_times" in d:
        auto["post_times"] = d["post_times"]
        auto["posts_per_day"] = max(1, min(10, len(d["post_times"]) or 1))
    if "posts_per_day" in d:
        ppd = max(1, min(10, int(d["posts_per_day"])))
        auto["posts_per_day"] = ppd
        # Regenerate the time slots the scheduler actually uses, unless the same
        # call already supplied explicit post_times.
        if "post_times" not in d:
            auto["post_times"] = _spread_times(ppd)
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
    """Hourly background task for Auto Pilot users.

    Auto Pilot NEVER stops itself for low credits — a task in progress always
    runs to completion and the balance is allowed to go negative (the debt is
    settled on the next top-up). Instead this task:
      • sends a low-credit reminder when the balance reaches 100, and again at 50
        (idempotent — each reminder fires once until the balance recovers above
        the threshold), and
      • triggers Stripe auto top-up for users who enabled it.
    """
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

    def _notify(uid: str, title: str, message: str):
        try:
            from routes.notifications import push_notification
            push_notification(uid, "credits_low", title, message)
        except Exception:
            pass

    while True:
        await asyncio.sleep(3600)  # run every hour

        # Refill balances first so reminders reflect the post-top-up balance.
        try:
            from routes.scheduler import check_auto_topup
            await check_auto_topup()
        except Exception:
            pass

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
                    tg_id = u.get("telegram_id") or ustore.get("telegram_chat_id")

                    rem = ustore.setdefault("credit_reminders", {})
                    changed = False

                    # Reset the one-shot flags once the balance recovers, so the
                    # reminders can fire again next time credits run low.
                    if credits > 100 and (rem.get("100") or rem.get("50")):
                        rem["100"] = False
                        rem["50"] = False
                        changed = True
                    elif credits > 50 and rem.get("50"):
                        rem["50"] = False
                        changed = True

                    # Reminder at 100 credits (only while above the 50 line).
                    if 50 < credits <= 100 and not rem.get("100"):
                        msg = (f"⚠️ UgoingViral: Auto Pilot is running low — {int(credits)} credits left. "
                               "Top up soon to avoid a negative balance. Auto Pilot keeps running.")
                        await _send_tg(tg_id, msg)
                        _notify(uid, "Credits running low (100)", msg)
                        rem["100"] = True
                        changed = True

                    # Reminder at 50 credits.
                    if credits <= 50 and not rem.get("50"):
                        msg = (f"⚠️ UgoingViral: Auto Pilot balance is very low — {int(credits)} credits. "
                               "Top up now. Auto Pilot will keep finishing tasks and the balance may go negative "
                               "(settled on your next top-up).")
                        await _send_tg(tg_id, msg)
                        _notify(uid, "Credits very low (50)", msg)
                        rem["50"] = True
                        changed = True

                    if changed:
                        ustore["credit_reminders"] = rem
                        _save_user_store(uid, ustore)
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

    # RETIRED: Full Auto Mode merged into True Auto Pilot (the Auto Pilot page).
    # Never enables a second autonomous engine — points the user to Auto Pilot.
    ustore["full_auto_enabled"] = False
    _save_user_store(uid, ustore)
    return {"ok": False, "deprecated": True,
            "message": "Full Auto Mode is now part of Auto Pilot — use the Auto Pilot page."}


async def run_full_auto_mode():
    # RETIRED. Full Auto Mode is superseded by content_team.autopilot (True Auto
    # Pilot), the single autonomous engine driven from the Auto Pilot page. This
    # loop is no longer started in app.py; kept as a no-op so any stray caller is
    # harmless and never double-generates/posts.
    return


async def _run_full_auto_mode_legacy_disabled():
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
