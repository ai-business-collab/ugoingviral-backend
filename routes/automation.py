from routes.auth import get_current_user
from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request, Depends
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log, _load_user_store, _save_user_store
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ── Automation ────────────────────────────────────────────────────────────────
@router.get("/api/automation/log")
def get_log(): return {"log": store.get("automation_log", {})}

# ── Twitter + YouTube API nøgler ─────────────────────────────────────────────
@router.post("/api/settings/twitter_api")
async def save_twitter_api(req: Request):
    d = await req.json()
    for k in ["twitter_api_key","twitter_api_secret","twitter_bearer_token","twitter_access_token","twitter_access_secret"]:
        if d.get(k): store.get("settings", {})[k] = d[k]
    store.get("settings", {})["twitter_api_connected"] = bool(d.get("twitter_api_key"))
    save_store(store)
    return {"status": "ok"}

@router.post("/api/settings/youtube_api")
async def save_youtube_api(req: Request):
    d = await req.json()
    for k in ["youtube_client_id","youtube_client_secret"]:
        if d.get(k): store.get("settings", {})[k] = d[k]
    save_store(store)
    return {"status": "ok"}

# ── Medie bibliotek ──────────────────────────────────────────────────────────
@router.get("/api/library/folders")
def get_lib_folders():
    return {"folders": store.get("lib_folders", [])}

@router.post("/api/library/folders")
async def create_lib_folder(req: Request):
    d = await req.json()
    name = d.get("name","").strip()
    if not name: return {"status":"error"}
    if "lib_folders" not in store: store["lib_folders"] = []
    if name not in store.get("lib_folders", {}):
        store.get("lib_folders", {}).append(name)
        save_store()
    return {"status":"ok", "folders": store.get("lib_folders", {})}

@router.delete("/api/library/folders/{name}")
def delete_lib_folder(name: str):
    store["lib_folders"] = [f for f in store.get("lib_folders",[]) if f != name]
    save_store()
    return {"status":"ok"}

@router.delete("/api/automation/log")
def clear_log():
    store["automation_log"] = []; save_store(); return {"status": "cleared"}

@router.post("/api/automation/generate_batch")
async def generate_batch():
    auto = store.get("automation", {})
    add_log("🚀 Starter batch generation...", "info")
    # Hent alle produkter — manual + Shopify cache
    all_prods = store.get("manual_products", []) + store.get("shopify_products_cache", [])
    if not all_prods:
        all_prods = _demo_products()
    # Filtrer kun produkter UDEN content
    has_content = set(str(k) for k in store.get("product_content", {}).keys() if store.get("product_content", {}).get(str(k)))
    products_without = [p for p in all_prods if str(p["id"]) not in has_content]
    if not products_without:
        add_log("✅ Alle produkter har allerede content", "info")
        return {"status": "done", "generated": 0, "message": "Alle produkter har content"}
    add_log(f"📦 {len(products_without)} produkter mangler content — genererer nu...", "info")
    products = products_without
    generated = 0
    for prod in products[:10]:
        active_platforms = [p for p, cfg in auto.get("platforms", {}).items() if cfg.get("active")]
        if not active_platforms: active_platforms = ["instagram"]
        for plat in active_platforms[:2]:
            for _ in range(auto.get("captions_per_product", 2)):
                req = ContentRequest(product_id=prod["id"], product_title=prod["title"],
                    product_description=prod.get("description", ""), content_type="caption",
                    platform=plat, language=auto.get("language", "da"), tone="engaging")
                await generate_content(req); generated += 1
            for _ in range(auto.get("hooks_per_product", 2)):
                req = ContentRequest(product_id=prod["id"], product_title=prod["title"],
                    content_type="hook", platform=plat, language=auto.get("language", "da"), tone="engaging")
                await generate_content(req); generated += 1
        add_log(f"✅ Content klar: {prod['title']}", "success")
    return {"status": "done", "generated": generated}


@router.get("/api/automation/dm_log")
def get_dm_log(current_user: dict = Depends(get_current_user)):
    ustore = _load_user_store(current_user["id"])
    log    = ustore.get("dm_scheduler_log", [])
    ctrs   = ustore.get("dm_scheduler_counters", {})
    return {"log": list(reversed(log[-50:])), "today": ctrs}


@router.post("/api/automation/dm_queue")
async def queue_dm_item(request: Request, current_user: dict = Depends(get_current_user)):
    """Queue an incoming DM or comment for auto-reply processing."""
    body   = await request.json()
    ustore = _load_user_store(current_user["id"])
    item   = {
        "sender_name": str(body.get("sender_name", ""))[:60],
        "message":     str(body.get("message", ""))[:500],
        "platform":    str(body.get("platform", ""))[:20],
        "queued_at":   datetime.utcnow().isoformat(),
    }
    if str(body.get("type", "dm")) == "comment":
        ustore.setdefault("pending_comments", []).append(item)
    else:
        ustore.setdefault("pending_dms", []).append(item)
    _save_user_store(current_user["id"], ustore)
    return {"ok": True}


# ══════════════════════════════════════════════════════════════════════════════
# AUTO DM + COMMENTS SCHEDULER  (runs every 30 min)
# ══════════════════════════════════════════════════════════════════════════════
async def run_auto_dm_scheduler():
    import asyncio as _asyncio
    import httpx as _httpx
    import os as _os
    from datetime import datetime as _dt, date as _date
    from services.store import (
        get_all_user_ids as _get_uids,
        _load_user_store as _lus,
        _save_user_store as _sus,
    )

    _GROWTH_PLUS = {"growth", "pro", "elite", "personal", "agency"}
    _MAX_DMS     = 50
    _MAX_CMTS    = 100

    async def _ai_reply(message, sender, dm_cfg, api_keys):
        lang   = {"da": "Danish", "en": "English"}.get(dm_cfg.get("reply_language", "en"), "English")
        tone   = dm_cfg.get("reply_tone", "friendly")
        ghost  = "You are a real person — never reveal you are an AI. " if dm_cfg.get("ghost_mode", True) else ""
        custom = ("Custom: " + dm_cfg["custom_instructions"]) if dm_cfg.get("custom_instructions") else ""
        avoid  = ("Do NOT mention: " + dm_cfg["forbidden_topics"]) if dm_cfg.get("forbidden_topics") else ""
        prompt = (
            f"Reply to {sender or 'customer'} on behalf of a business. "
            f"Tone: {tone}. Language: {lang}. Max 2 sentences. Emojis welcome. "
            f"{ghost}{custom} {avoid}\nMessage: \"{message}\"\nReply:"
        )
        ak = api_keys.get("anthropic_key", "") or _os.getenv("ANTHROPIC_API_KEY", "")
        if ak and "\u2022\u2022" not in ak:
            try:
                async with _httpx.AsyncClient(timeout=20) as c:
                    r = await c.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": ak, "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 150,
                              "messages": [{"role": "user", "content": prompt}]},
                    )
                    r.raise_for_status()
                    return r.json()["content"][0]["text"].strip()
            except Exception:
                pass
        ok = api_keys.get("openai_key", "") or _os.getenv("OPENAI_API_KEY", "")
        if ok and "\u2022\u2022" not in ok:
            try:
                async with _httpx.AsyncClient(timeout=20) as c:
                    r = await c.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {ok}", "content-type": "application/json"},
                        json={"model": "gpt-4o-mini", "max_tokens": 120,
                              "messages": [{"role": "user", "content": prompt}]},
                    )
                    r.raise_for_status()
                    return r.json()["choices"][0]["message"]["content"].strip()
            except Exception:
                pass
        return "Thank you for reaching out! \U0001F604 We\'ll be in touch soon."

    while True:
        await _asyncio.sleep(1800)
        today = _date.today().isoformat()
        try:
            uids = _get_uids()
        except Exception:
            continue

        for uid in uids:
            try:
                ustore  = _lus(uid)
                plan    = ustore.get("billing", {}).get("plan", "free")
                if plan not in _GROWTH_PLUS:
                    continue

                auto_cfg    = ustore.get("automation", {}).get("platforms", {})
                dm_on       = any(v.get("auto_dm")       for v in auto_cfg.values())
                cmt_on      = any(v.get("auto_comments") for v in auto_cfg.values())
                if not dm_on and not cmt_on:
                    continue

                dm_cfg   = ustore.get("dm_settings", {})
                settings = ustore.get("settings", {})
                api_keys = {
                    "anthropic_key": settings.get("anthropic_key", ""),
                    "openai_key":    settings.get("openai_key", ""),
                }

                ctrs = ustore.setdefault("dm_scheduler_counters", {})
                if ctrs.get("date") != today:
                    ctrs.update({"date": today, "dms_sent": 0, "cmts_sent": 0})

                log = ustore.setdefault("dm_scheduler_log", [])
                dirty = False

                if dm_on and ctrs["dms_sent"] < _MAX_DMS:
                    pending = ustore.get("pending_dms", [])
                    cap     = _MAX_DMS - ctrs["dms_sent"]
                    batch, leftover = pending[:cap], pending[cap:]
                    for item in batch:
                        try:
                            reply = await _ai_reply(
                                item.get("message", ""), item.get("sender_name", ""),
                                dm_cfg, api_keys)
                            ctrs["dms_sent"] += 1
                            log.append({"ts": _dt.utcnow().isoformat(), "type": "dm",
                                        "platform": item.get("platform",""),
                                        "sender": item.get("sender_name",""),
                                        "reply": reply[:100], "status": "sent"})
                            dirty = True
                        except Exception:
                            leftover.insert(0, item)
                    ustore["pending_dms"] = leftover

                if cmt_on and ctrs["cmts_sent"] < _MAX_CMTS:
                    pending = ustore.get("pending_comments", [])
                    cap     = _MAX_CMTS - ctrs["cmts_sent"]
                    batch, leftover = pending[:cap], pending[cap:]
                    for item in batch:
                        try:
                            reply = await _ai_reply(
                                item.get("message", ""), item.get("sender_name", ""),
                                dm_cfg, api_keys)
                            ctrs["cmts_sent"] += 1
                            log.append({"ts": _dt.utcnow().isoformat(), "type": "comment",
                                        "platform": item.get("platform",""),
                                        "sender": item.get("sender_name",""),
                                        "reply": reply[:100], "status": "sent"})
                            dirty = True
                        except Exception:
                            leftover.insert(0, item)
                    ustore["pending_comments"] = leftover

                ustore["dm_scheduler_log"]      = log[-200:]
                ustore["dm_scheduler_counters"] = ctrs
                if dirty:
                    _sus(uid, ustore)
            except Exception:
                continue
