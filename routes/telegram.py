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
        scheduled = len([p for p in ustore.get("scheduled_posts", []) if not p.get("posted")])
        auto_active = ustore.get("automation", {}).get("active", False)
        auto_icon = "ON" if auto_active else "OFF"
        msg = (
            "*Welcome to UgoingViral!*\n\n"
            f"Auto Pilot: *{auto_icon}*\n"
            f"Plan: *{plan_name}*\n"
            f"Credits: *{credits}*\n"
            f"Queued posts: *{scheduled}*\n\n"
            "*Commands:*\n"
            "/stats - today's overview\n"
            "/credits - credit balance\n"
            "/autopilot on|off - toggle auto pilot\n"
            "/post - quick post info\n"
            "/help - all commands"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/stats":
        today = datetime.utcnow().strftime("%Y-%m-%d")
        posts = ustore.get("scheduled_posts", [])
        today_posts = sum(1 for p in posts if p.get("scheduled_time", "").startswith(today))
        total_posted = sum(1 for p in posts if p.get("posted"))
        history = ustore.get("content_history", [])
        content_today = sum(1 for h in history if h.get("created_at", h.get("ts", "")).startswith(today))
        usage = ustore.get("api_usage", [])
        credits_today = sum(e.get("credits", 0) for e in usage if e.get("ts", "").startswith(today))
        msg = (
            f"*Today's Stats* - {today}\n\n"
            f"Posts scheduled today: *{today_posts}*\n"
            f"Total posted (all time): *{total_posted}*\n"
            f"Content generated today: *{content_today}*\n"
            f"Credits used today: *{credits_today}*\n"
            f"Credits remaining: *{credits}*\n"
            f"Plan: *{plan_name}*"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/credits":
        plan_info = PLANS.get(plan_key, PLANS["free"])
        max_cr = plan_info["credits"]
        pct = int(100 * credits / max_cr) if max_cr > 0 else 0
        filled = int(pct / 10)
        bar = "X" * filled + "-" * (10 - filled)
        msg = (
            "*Credit Balance*\n\n"
            f"`[{bar}]` {pct}%\n"
            f"*{credits}* / {max_cr} credits\n\n"
            f"Plan: *{plan_name}*\n"
            "Top-up: https://ugoingviral.com/app"
        )
        await tg_send(chat_id, msg)

    elif cmd == "/autopilot":
        arg = args.strip().lower()
        automation = ustore.setdefault("automation", {})
        if arg in ("on", "off"):
            new_state = arg == "on"
            automation["active"] = new_state
            from services.store import _save_user_store
            _save_user_store(user_id, ustore)
            add_log(f"Telegram: Auto Pilot {arg.upper()}", "info")
            status_word = "ENABLED" if new_state else "DISABLED"
            desc = "posting at scheduled times" if new_state else "stopped automatic posting"
            await tg_send(chat_id, f"*Auto Pilot {status_word}*\n\nUgoingViral will now {desc}.")
        else:
            current = automation.get("active", False)
            current_word = "ON" if current else "OFF"
            await tg_send(chat_id, f"Auto Pilot is currently *{current_word}*\n\nChange it: `/autopilot on` or `/autopilot off`")

    elif cmd == "/post":
        await tg_send(chat_id, (
            "*Quick Post*\n\n"
            "Use the Content Engine to generate posts:\n"
            "https://ugoingviral.com/app\n\n"
            "Go to Content Engine for a 30-day calendar."
        ))

    elif cmd in ("/help", "/menu"):
        await tg_send(chat_id, (
            "*UgoingViral Bot - Commands*\n\n"
            "/start - welcome and stats\n"
            "/stats - today's performance\n"
            "/credits - credit balance\n"
            "/autopilot on|off - toggle auto pilot\n"
            "/post - quick post info\n"
            "/help - this menu\n\n"
            "Dashboard: https://ugoingviral.com/app"
        ))
    else:
        await tg_send(chat_id, f"Unknown command: `{cmd}`\n\nType /help for available commands.")


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
