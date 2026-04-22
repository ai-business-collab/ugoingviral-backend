from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ── Automation ────────────────────────────────────────────────────────────────
@router.get("/api/automation/log")
def get_log(): return {"log": store["automation_log"]}

# ── Twitter + YouTube API nøgler ─────────────────────────────────────────────
@router.post("/api/settings/twitter_api")
async def save_twitter_api(req: Request):
    d = await req.json()
    for k in ["twitter_api_key","twitter_api_secret","twitter_bearer_token","twitter_access_token","twitter_access_secret"]:
        if d.get(k): store["settings"][k] = d[k]
    store["settings"]["twitter_api_connected"] = bool(d.get("twitter_api_key"))
    save_store(store)
    return {"status": "ok"}

@router.post("/api/settings/youtube_api")
async def save_youtube_api(req: Request):
    d = await req.json()
    for k in ["youtube_client_id","youtube_client_secret"]:
        if d.get(k): store["settings"][k] = d[k]
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
    if name not in store["lib_folders"]:
        store["lib_folders"].append(name)
        save_store()
    return {"status":"ok", "folders": store["lib_folders"]}

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
    auto = store["automation"]
    add_log("🚀 Starter batch generation...", "info")
    # Hent alle produkter — manual + Shopify cache
    all_prods = store.get("manual_products", []) + store.get("shopify_products_cache", [])
    if not all_prods:
        all_prods = _demo_products()
    # Filtrer kun produkter UDEN content
    has_content = set(str(k) for k in store.get("product_content", {}).keys() if store["product_content"].get(str(k)))
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
