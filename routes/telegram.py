"""
UgoingViral — Telegram Bot
POST /api/telegram/webhook   — receive Telegram updates
GET  /api/telegram/setup     — register webhook URL with Telegram
GET  /api/telegram/status    — check bot connection
POST /api/telegram/generate-link-token — link UgoingViral account
"""
import os, json
from datetime import datetime
from fastapi import APIRouter, Request, Depends
from routes.auth import get_current_user
from services.store import store, save_store, add_log

router = APIRouter()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_API = "https://api.telegram.org/bot"


async def tg_send(chat_id, text: str, parse_mode="Markdown"):
    if not BOT_TOKEN:
        return
    import httpx
    payload = {"chat_id": chat_id, "text": text, "parse_mode": parse_mode}
    try:
        async with httpx.AsyncClient() as c:
            await c.post(f"{TG_API}{BOT_TOKEN}/sendMessage", json=payload, timeout=10)
    except Exception as e:
        add_log(f"Telegram send error: {e}", "error")


def _find_user_by_tg(tg_id: int):
    from services.users import load_users
    data = load_users()
    for u in data.get("users", []):
        if u.get("telegram_id") == tg_id:
            return u
    return None


def _get_user_store(user_id: str):
    from services.store import _load_user_store
    return _load_user_store(user_id)


async def _handle_command(cmd: str, chat_id: int, user_id: str, args: str = ""):
    ustore = _get_user_store(user_id)
    billing = ustore.get("billing", {})
    from routes.billing import PLANS
    plan_key = billing.get("plan", "free")
    credits = billing.get("credits", 0)
    plan_name = PLANS.get(plan_key, {}).get("name", plan_key.capitalize())

    if cmd == "/start":
        # Support deep link: /start TOKEN links account automatically
        if args.strip():
            from services.users import load_users, save_users
            data = load_users()
            for u in data["users"]:
                if u.get("tg_link_token") == args.strip():
                    u["telegram_id"] = chat_id
                    u["telegram_username"] = ""
                    u.pop("tg_link_token", None)
                    save_users(data)
                    name = u.get("name") or u["email"]
                    await tg_send(chat_id, f"*Account linked!* ✅\n\nWelcome, {name}!\nType /status to see your account.")
                    return
        scheduled = len([p for p in ustore.get("scheduled_posts", []) if not p.get("posted")])
        auto_active = ustore.get("automation", {}).get("active", False)
        auto_icon = "ON 🟢" if auto_active else "OFF 🔴"
        msg = (
            "*Welcome to UgoingViral!* 🚀\n\n"
            f"Auto Pilot: *{auto_icon}*\n"
            f"Plan: *{plan_name}*\n"
            f"Credits: *{credits}*\n"
            f"Queued posts: *{scheduled}*\n\n"
            "*Commands:*\n"
            "/status — account overview\n"
            "/stats — 7-day performance\n"
            "/plan — current content plan\n"
            "/credits — credit balance\n"
            "/pause — pause autopilot\n"
            "/resume — resume autopilot\n"
            "/help — all commands"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/status":
        scheduled = ustore.get("scheduled_posts", [])
        queued = len([p for p in scheduled if not p.get("posted")])
        auto_active = ustore.get("automation", {}).get("active", False)
        auto_icon = "ON 🟢" if auto_active else "OFF 🔴"
        s = ustore.get("settings", {})
        plats = s.get("platforms", {})
        connected_plats = [k for k, v in plats.items() if v and v.get("active")]
        plat_str = ", ".join(connected_plats) if connected_plats else "none connected"
        plan_info = PLANS.get(plan_key, PLANS["free"])
        max_cr = plan_info.get("credits", 0)
        pct = int(100 * credits / max_cr) if max_cr > 0 else 0
        msg = (
            "*Account Status* 📊\n\n"
            f"Plan: *{plan_name}*\n"
            f"Credits: *{credits}/{max_cr}* ({pct}%)\n"
            f"Auto Pilot: *{auto_icon}*\n"
            f"Queued posts: *{queued}*\n"
            f"Platforms: {plat_str}\n\n"
            "Dashboard: https://ugoingviral.com/app"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/stats":
        from datetime import timedelta
        today = datetime.utcnow()
        week_ago = today - timedelta(days=7)
        posts = ustore.get("scheduled_posts", [])
        week_posts = sum(1 for p in posts if p.get("posted") and p.get("posted_at", p.get("scheduled_time", "")) >= week_ago.strftime("%Y-%m-%d"))
        today_str = today.strftime("%Y-%m-%d")
        today_posts = sum(1 for p in posts if p.get("scheduled_time", "").startswith(today_str))
        history = ustore.get("content_history", [])
        week_content = sum(1 for h in history if h.get("created_at", h.get("ts", "")) >= week_ago.strftime("%Y-%m-%d"))
        usage = ustore.get("api_usage", [])
        week_credits = sum(e.get("credits", 0) for e in usage if e.get("ts", "") >= week_ago.strftime("%Y-%m-%d"))
        msg = (
            f"*7-Day Stats* 📈\n\n"
            f"Posts published: *{week_posts}*\n"
            f"Scheduled today: *{today_posts}*\n"
            f"Content generated: *{week_content}*\n"
            f"Credits used: *{week_credits}*\n"
            f"Credits remaining: *{credits}*\n"
            f"Plan: *{plan_name}*"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/plan":
        plans_list = ustore.get("content_plans", [])
        if not plans_list:
            await tg_send(chat_id, "No content plan found.\n\nGenerate one at: https://ugoingviral.com/app")
            return
        plan = plans_list[0]
        niche = plan.get("niche", "")
        goal = plan.get("goal", "")
        days = plan.get("days", [])
        created = plan.get("created_at", "")[:10]
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        today_day = None
        for d in days:
            if d.get("date", "") == today_str:
                today_day = d
                break
        today_section = ""
        if today_day:
            posts_today = today_day.get("posts", [])
            post_lines = []
            for p in posts_today[:3]:
                t = p.get("best_time", "09:00")
                pt = p.get("post_type", "post")
                plat = p.get("platform", "")
                post_lines.append(f"  • {t} — {pt} on {plat}")
            today_section = "\n*Today\'s posts:*\n" + "\n".join(post_lines)
        msg = (
            f"*Current Content Plan* 🗓️\n\n"
            f"Created: {created}\n"
            f"Niche: {niche}\n"
            f"Goal: {goal}\n"
            f"Days planned: {len(days)}"
            f"{today_section}\n\n"
            "Full plan: https://ugoingviral.com/app"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/credits":
        plan_info = PLANS.get(plan_key, PLANS["free"])
        max_cr = plan_info.get("credits", 0)
        pct = int(100 * credits / max_cr) if max_cr > 0 else 0
        filled = int(pct / 10)
        bar = "█" * filled + "░" * (10 - filled)
        low_note = "\n⚠️ *Low credits!* Top up soon." if credits < 100 else ""
        msg = (
            "*Credit Balance* 💳\n\n"
            f"`[{bar}]` {pct}%\n"
            f"*{credits}* / {max_cr} credits{low_note}\n\n"
            f"Plan: *{plan_name}*\n"
            "Top-up: https://ugoingviral.com/app"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/pause":
        automation = ustore.setdefault("automation", {})
        automation["active"] = False
        from services.store import _save_user_store
        _save_user_store(user_id, ustore)
        await tg_send(chat_id, "⏸️ *Autopilot paused.*\n\nNo new posts will be published automatically.\nType /resume to re-enable.")

    elif cmd == "/resume":
        automation = ustore.setdefault("automation", {})
        automation["active"] = True
        from services.store import _save_user_store
        _save_user_store(user_id, ustore)
        await tg_send(chat_id, "▶️ *Autopilot resumed.*\n\nPosts will be published at scheduled times.")

    elif cmd == "/autopilot":
        arg = args.strip().lower()
        automation = ustore.setdefault("automation", {})
        if arg in ("on", "off"):
            new_state = arg == "on"
            automation["active"] = new_state
            from services.store import _save_user_store
            _save_user_store(user_id, ustore)
            add_log(f"Telegram: Auto Pilot {arg.upper()}", "info")
            status_word = "ENABLED 🟢" if new_state else "DISABLED 🔴"
            await tg_send(chat_id, f"*Auto Pilot {status_word}*")
        else:
            current = automation.get("active", False)
            current_word = "ON 🟢" if current else "OFF 🔴"
            await tg_send(chat_id, f"Auto Pilot is currently *{current_word}*\n\n`/autopilot on` or `/autopilot off`\nOr use /pause and /resume")

    elif cmd in ("/help", "/menu"):
        await tg_send(chat_id, (
            "*UgoingViral Bot Commands* 🤖\n\n"
            "/status — account overview\n"
            "/stats — 7-day performance\n"
            "/plan — current content plan\n"
            "/credits — credit balance\n"
            "/pause — pause autopilot\n"
            "/resume — resume autopilot\n"
            "/autopilot on|off — toggle autopilot\n"
            "/help — this menu\n\n"
            "You can also send natural language:\n"
            "_\"post my latest product tomorrow at 9am\"_\n\n"
            "Dashboard: https://ugoingviral.com/app"
        ))
    else:
        # Natural language fallback
        await _handle_natural_language(cmd + " " + args, chat_id, user_id, ustore)


async def _handle_natural_language(text: str, chat_id: int, user_id: str, ustore: dict):
    """Simple NLP for scheduling commands."""
    text_lower = text.lower().strip()

    # Detect scheduling intent
    schedule_keywords = ["post", "schedule", "publish", "share", "send"]
    time_keywords = ["tomorrow", "today", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                     "at ", "am", "pm", "9am", "10am", "noon", "morning", "evening"]

    has_schedule = any(k in text_lower for k in schedule_keywords)
    has_time = any(k in text_lower for k in time_keywords)

    if has_schedule and has_time:
        # Parse time
        from datetime import timedelta
        import re
        now = datetime.utcnow()
        target_dt = now

        if "tomorrow" in text_lower:
            target_dt = now + timedelta(days=1)
        elif "today" in text_lower:
            target_dt = now

        # Parse hour
        hour_match = re.search(r"(\d{1,2})\s*(am|pm)", text_lower)
        if hour_match:
            h = int(hour_match.group(1))
            if hour_match.group(2) == "pm" and h < 12:
                h += 12
            elif hour_match.group(2) == "am" and h == 12:
                h = 0
            target_dt = target_dt.replace(hour=h, minute=0, second=0, microsecond=0)
        elif "morning" in text_lower:
            target_dt = target_dt.replace(hour=9, minute=0, second=0, microsecond=0)
        elif "noon" in text_lower:
            target_dt = target_dt.replace(hour=12, minute=0, second=0, microsecond=0)
        elif "evening" in text_lower:
            target_dt = target_dt.replace(hour=18, minute=0, second=0, microsecond=0)

        # Find latest product or content
        products = ustore.get("manual_products", [])
        history = ustore.get("content_history", [])
        s = ustore.get("settings", {})
        plats = s.get("platforms", {})
        platform = next((k for k, v in plats.items() if v and v.get("active")), "instagram")

        caption = ""
        if "product" in text_lower and products:
            prod = products[-1]
            caption = f"Check out {prod.get('name', 'our latest product')}! 🛍️ {prod.get('description', '')[:80]}"
        elif history:
            caption = history[-1].get("caption", history[-1].get("content", ""))[:200]

        if not caption:
            await tg_send(chat_id, "I understood you want to schedule a post, but I couldn\'t find content to post. Please generate content first at https://ugoingviral.com/app")
            return

        # Add to scheduled posts
        scheduled = ustore.setdefault("scheduled_posts", [])
        import uuid
        post = {
            "id": uuid.uuid4().hex[:8],
            "platform": platform,
            "content": caption,
            "scheduled_time": target_dt.strftime("%Y-%m-%dT%H:%M:00"),
            "posted": False,
            "source": "telegram_nlp"
        }
        scheduled.append(post)
        from services.store import _save_user_store
        _save_user_store(user_id, ustore)

        dt_str = target_dt.strftime("%b %d at %H:%M UTC")
        await tg_send(chat_id, (
            f"✅ *Post scheduled!*\n\n"
            f"Platform: {platform}\n"
            f"Time: {dt_str}\n"
            f"Preview: _{caption[:100]}..._\n\n"
            "View in dashboard: https://ugoingviral.com/app"
        ))
        return

    # Credits/balance query
    if any(k in text_lower for k in ["credit", "balance", "how many"]):
        await _handle_command("/credits", chat_id, user_id)
        return

    # Status query
    if any(k in text_lower for k in ["status", "account", "plan"]):
        await _handle_command("/status", chat_id, user_id)
        return

    await tg_send(chat_id, (
        "I didn\'t understand that. Try:\n\n"
        "• `/status` — account overview\n"
        "• `/stats` — 7-day stats\n"
        "• `/credits` — balance\n"
        "• _\"post my latest product tomorrow at 9am\"_\n\n"
        "/help for all commands"
    ))


@router.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    if not BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    try:
        update = await request.json()
    except Exception:
        return {"ok": False}

    message = update.get("message") or update.get("edited_message")
    if not message:
        return {"ok": True}

    chat_id = message.get("chat", {}).get("id")
    tg_user_id = message.get("from", {}).get("id")
    text = (message.get("text") or "").strip()

    if not text or not chat_id:
        return {"ok": True}

    parts = text.split(None, 1)
    cmd = parts[0].lower().split("@")[0]
    args = parts[1] if len(parts) > 1 else ""

    ugv_user = _find_user_by_tg(tg_user_id)

    if not ugv_user:
        if cmd == "/link" and args:
            from services.users import load_users, save_users
            data = load_users()
            linked = False
            for u in data["users"]:
                if u.get("tg_link_token") == args.strip():
                    u["telegram_id"] = tg_user_id
                    u["telegram_username"] = message.get("from", {}).get("username", "")
                    u.pop("tg_link_token", None)
                    linked = True
                    save_users(data)
                    name = u.get("name") or u["email"]
                    await tg_send(chat_id, f"*Account linked!*\n\nWelcome, {name}!\nType /start to begin.")
                    break
            if not linked:
                await tg_send(chat_id, "Invalid link token. Get a new one from UgoingViral Settings.")
        else:
            await tg_send(chat_id, (
                "*Welcome to UgoingViral Bot!*\n\n"
                "To get started, link your account:\n\n"
                "1. Go to https://ugoingviral.com/app\n"
                "2. Open Settings - Telegram\n"
                "3. Copy your link token\n"
                "4. Send: `/link YOUR_TOKEN`"
            ))
        return {"ok": True}

    await _handle_command(cmd, chat_id, ugv_user["id"], args)
    return {"ok": True}


@router.get("/api/telegram/setup")
async def setup_webhook(current_user: dict = Depends(get_current_user)):
    if not BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not set in .env"}
    import httpx
    webhook_url = "https://ugoingviral.com/api/telegram/webhook"
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{TG_API}{BOT_TOKEN}/setWebhook",
            json={"url": webhook_url, "allowed_updates": ["message"]},
            timeout=10,
        )
        result = r.json()
    if result.get("ok"):
        add_log(f"Telegram webhook registered: {webhook_url}", "success")
    return result


@router.get("/api/telegram/status")
async def telegram_status(current_user: dict = Depends(get_current_user)):
    if not BOT_TOKEN:
        return {"ok": False, "configured": False, "error": "TELEGRAM_BOT_TOKEN not set in .env"}
    import httpx
    try:
        async with httpx.AsyncClient() as c:
            bot_info = await c.get(f"{TG_API}{BOT_TOKEN}/getMe", timeout=8)
            hook_info = await c.get(f"{TG_API}{BOT_TOKEN}/getWebhookInfo", timeout=8)
        bot = bot_info.json()
        hook = hook_info.json()
        from services.users import get_user_by_id
        u = get_user_by_id(current_user["id"])
        linked = bool(u and u.get("telegram_id"))
        return {
            "ok": bot.get("ok", False),
            "configured": True,
            "bot_username": bot.get("result", {}).get("username"),
            "bot_name": bot.get("result", {}).get("first_name"),
            "webhook_url": hook.get("result", {}).get("url"),
            "pending_updates": hook.get("result", {}).get("pending_update_count", 0),
            "linked": linked,
        }
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}


@router.post("/api/telegram/generate-link-token")
async def generate_link_token(current_user: dict = Depends(get_current_user)):
    import secrets
    from services.users import update_user
    token = secrets.token_urlsafe(16)
    update_user(current_user["id"], {"tg_link_token": token})
    bot_username = None
    if BOT_TOKEN:
        try:
            import httpx
            async with httpx.AsyncClient() as c:
                r = await c.get(f"{TG_API}{BOT_TOKEN}/getMe", timeout=5)
                bot_username = r.json().get("result", {}).get("username")
        except Exception:
            pass
    return {
        "token": token,
        "bot_username": bot_username,
        "link_command": f"/link {token}",
        "bot_url": f"https://t.me/{bot_username}" if bot_username else None,
    }


@router.delete("/api/telegram/unlink")
async def unlink_telegram(current_user: dict = Depends(get_current_user)):
    from services.users import update_user
    update_user(current_user["id"], {"telegram_id": None, "telegram_username": None, "tg_link_token": None})
    return {"ok": True}


# ── User-facing send endpoint ─────────────────────────────────────────────────

@router.post("/api/telegram/send")
async def send_telegram_message(request: Request, current_user: dict = Depends(get_current_user)):
    if not BOT_TOKEN:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN not configured"}
    body = await request.json()
    text = body.get("text", "").strip()
    if not text:
        return {"ok": False, "error": "text is required"}
    from services.users import get_user_by_id
    u = get_user_by_id(current_user["id"])
    tg_id = u and u.get("telegram_id")
    if not tg_id:
        return {"ok": False, "error": "Telegram not connected. Link your account first."}
    await tg_send(tg_id, text)
    return {"ok": True}


@router.post("/api/telegram/connect")
async def connect_telegram(request: Request, current_user: dict = Depends(get_current_user)):
    """Save a telegram chat_id directly to the user profile."""
    body = await request.json()
    chat_id_val = body.get("chat_id")
    if not chat_id_val:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="chat_id is required")
    from services.users import update_user
    update_user(current_user["id"], {"telegram_id": int(chat_id_val)})
    return {"ok": True}


@router.get("/api/telegram/daily-report-settings")
async def get_daily_report_settings(current_user: dict = Depends(get_current_user)):
    tg_prefs = store.get("telegram_prefs", {})
    return {
        "daily_report_hour": tg_prefs.get("daily_report_hour", 8),
        "notify_low_credits": tg_prefs.get("notify_low_credits", True),
        "notify_daily_summary": tg_prefs.get("notify_daily_summary", True),
        "notify_post_success": tg_prefs.get("notify_post_success", False),
        "notify_post_failure": tg_prefs.get("notify_post_failure", True),
    }


@router.post("/api/telegram/daily-report-settings")
async def save_daily_report_settings(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    tg_prefs = store.get("telegram_prefs", {})
    if "daily_report_hour" in body:
        tg_prefs["daily_report_hour"] = max(0, min(23, int(body["daily_report_hour"])))
    for key in ("notify_low_credits", "notify_daily_summary", "notify_post_success", "notify_post_failure"):
        if key in body:
            tg_prefs[key] = bool(body[key])
    store["telegram_prefs"] = tg_prefs
    save_store()
    return {"ok": True}


# ── Daily report background task ──────────────────────────────────────────────

_last_report_day: dict = {}   # user_id → date string


async def _send_daily_report(user_id: str, tg_id: int):
    """Send a daily summary to a user's Telegram."""
    from services.store import _load_user_store
    ustore = _load_user_store(user_id)
    billing = ustore.get("billing", {})
    from routes.billing import PLANS
    plan_key = billing.get("plan", "free")
    credits = billing.get("credits", 0)
    plan_name = PLANS.get(plan_key, {}).get("name", plan_key.capitalize())

    from datetime import timedelta
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    posts = ustore.get("scheduled_posts", [])
    yesterday_posted = sum(1 for p in posts if p.get("posted") and
                           p.get("scheduled_time", "").startswith(yesterday))
    today_scheduled = [p for p in posts if not p.get("posted") and
                       p.get("scheduled_time", "").startswith(today_str)]

    usage = ustore.get("api_usage", [])
    credits_yesterday = sum(e.get("credits", 0) for e in usage if e.get("ts", "").startswith(yesterday))

    today_lines = []
    for p in today_scheduled[:4]:
        t = p.get("scheduled_time", "")[-8:][:5]
        plat = p.get("platform", "")
        today_lines.append(f"  {t} — {plat}")
    today_section = "\n".join(today_lines) if today_lines else "  Nothing scheduled"

    low_cr_alert = ""
    tg_prefs = ustore.get("telegram_prefs", {})
    if tg_prefs.get("notify_low_credits", True) and credits < 100:
        low_cr_alert = f"\n⚠️ *Low credits alert!* Only *{credits}* credits remaining. [Top up →](https://ugoingviral.com/app)"

    if not tg_prefs.get("notify_daily_summary", True):
        return

    msg = (
        f"*☀️ Daily Report — {today_str}*\n\n"
        f"Posts sent yesterday: *{yesterday_posted}*\n"
        f"Credits used yesterday: *{credits_yesterday}*\n"
        f"Credits remaining: *{credits}*\n\n"
        f"*Today\'s schedule:*\n{today_section}"
        f"{low_cr_alert}\n\n"
        "https://ugoingviral.com/app"
    )
    await tg_send(tg_id, msg)


async def run_daily_reports():
    """Background task — sends daily reports at user-configured hour."""
    import asyncio
    from services.users import load_users
    while True:
        try:
            now = datetime.utcnow()
            data = load_users()
            for u in data.get("users", []):
                tg_id = u.get("telegram_id")
                if not tg_id:
                    continue
                uid = u["id"]
                from services.store import _load_user_store
                ustore = _load_user_store(uid)
                tg_prefs = ustore.get("telegram_prefs", {})
                report_hour = tg_prefs.get("daily_report_hour", 8)
                today_str = now.strftime("%Y-%m-%d")

                if now.hour == report_hour and _last_report_day.get(uid) != today_str:
                    _last_report_day[uid] = today_str
                    try:
                        await _send_daily_report(uid, tg_id)
                    except Exception as e:
                        add_log(f"Daily report error for {uid}: {e}", "error")

                # Low-credit alert independent of daily report time
                if tg_prefs.get("notify_low_credits", True):
                    credits = ustore.get("billing", {}).get("credits", 9999)
                    alert_key = f"low_cr_alert_{today_str}"
                    if credits < 100 and not ustore.get(alert_key):
                        try:
                            await tg_send(tg_id, f"⚠️ *Low Credits Alert*\n\nYou have only *{credits}* credits left. Top up to keep your content running: https://ugoingviral.com/app")
                            from services.store import _save_user_store
                            ustore[alert_key] = True
                            _save_user_store(uid, ustore)
                        except Exception:
                            pass
        except Exception as e:
            add_log(f"run_daily_reports error: {e}", "error")
        await asyncio.sleep(3600)  # check every hour
