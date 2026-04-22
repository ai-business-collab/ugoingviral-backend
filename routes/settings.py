from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ── Health ────────────────────────────────────────────────────────────────────
@router.get("/health")
def health():
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "version": "4.0"}

# ── Settings ──────────────────────────────────────────────────────────────────
@router.get("/api/settings")
def get_settings():
    s = store["settings"].copy()
    for k in ["shopify_token", "instagram_pass", "tiktok_pass", "facebook_pass",
              "twitter_pass", "youtube_pass", "anthropic_key", "openai_key"]:
        if s.get(k): s[k] = s[k][:4] + "••••"
    return s

@router.post("/api/settings")
def save_settings(settings: Settings):
    d = settings.dict()
    for k, v in d.items():
        if v is not None and not str(v).endswith("••••"):
            store["settings"][k] = v
    # Auto bonus: first social media connection
    try:
        ig = d.get("instagram_user") or store["settings"].get("instagram_user")
        tt = d.get("tiktok_user") or store["settings"].get("tiktok_user")
        if ig or tt:
            from routes.billing import _try_auto_bonus
            _try_auto_bonus("connect_social")
    except Exception:
        pass
    save_store()
    return {"status": "saved"}

@router.post("/api/settings/toggle")
def toggle_api(t: ApiToggle):
    store["settings"]["api_enabled"][t.platform] = t.enabled
    save_store()
    return {"status": "ok"}

@router.delete("/api/settings/{platform}")
def delete_connection(platform: str):
    mapping = {
        "shopify": ["shopify_store", "shopify_token"],
        "instagram": ["instagram_user", "instagram_pass"],
        "tiktok": ["tiktok_user", "tiktok_pass"],
        "facebook": ["facebook_user", "facebook_pass"],
        "twitter": ["twitter_user", "twitter_pass"],
        "youtube": ["youtube_user", "youtube_pass"],
        "ai": ["anthropic_key", "openai_key"],
    }
    for key in mapping.get(platform, []):
        store["settings"][key] = ""
    save_store()
    return {"status": "deleted"}

@router.get("/api/settings/connections")
def get_connections():
    s = store["settings"]
    conns = {
        "shopify": bool(s.get("shopify_store") and s.get("shopify_token")),
        "ai": bool(s.get("anthropic_key") or s.get("openai_key")),
        "api_enabled": s.get("api_enabled", {}),
    }
    for p in ["instagram", "tiktok", "facebook", "twitter", "youtube"]:
        conns[p] = bool(s.get(f"{p}_user") and s.get(f"{p}_pass"))
    return conns

# ── Automation settings ───────────────────────────────────────────────────────
@router.get("/api/automation/settings")
def get_auto(): return store["automation"]

@router.post("/api/automation/settings")
def save_auto(s: AutomationSettings):
    for k, v in s.dict().items():
        if v is not None: store["automation"][k] = v
    save_store()
    return {"status": "saved", "settings": store["automation"]}

@router.post("/api/automation/platform")
def set_platform_automation(p: PlatformAutomation):
    if "platforms" not in store["automation"]:
        store["automation"]["platforms"] = {}
    if p.platform not in store["automation"]["platforms"]:
        store["automation"]["platforms"][p.platform] = {"active": False, "auto_post": False, "auto_dm": False, "auto_comments": False}
    # Fix: platforms kan være liste eller dict
    if not isinstance(store["automation"]["platforms"], dict):
        store["automation"]["platforms"] = {}
    if p.platform not in store["automation"]["platforms"]:
        store["automation"]["platforms"][p.platform] = {}
    if p.active is not None: store["automation"]["platforms"][p.platform]["active"] = p.active
    if p.auto_post is not None: store["automation"]["platforms"][p.platform]["auto_post"] = p.auto_post
    if p.auto_dm is not None: store["automation"]["platforms"][p.platform]["auto_dm"] = p.auto_dm
    if p.auto_comments is not None: store["automation"]["platforms"][p.platform]["auto_comments"] = p.auto_comments
    save_store()
    return {"status": "saved"}

# ── DM settings ───────────────────────────────────────────────────────────────
@router.get("/api/dm/settings")
def get_dm(): return store["dm_settings"]

@router.post("/api/dm/settings")
def save_dm(s: DMSettings):
    for k, v in s.dict().items():
        if v is not None: store["dm_settings"][k] = v
    save_store()
    return {"status": "saved"}

# ── Image upload ──────────────────────────────────────────────────────────────
@router.post("/api/upload/image")
async def upload_image(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["jpg", "jpeg", "png", "webp", "gif"]:
        raise HTTPException(400, "Kun billeder (jpg, png, webp, gif)")
    filename = f"{datetime.now().timestamp()}.{ext}"
    with open(f"uploads/{filename}", "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"url": f"http://localhost:8000/uploads/{filename}", "filename": filename}

@router.post("/api/upload/creator-image")
async def upload_creator_image(file: UploadFile = File(...)):
    ext = file.filename.split(".")[-1].lower()
    if ext not in ["jpg", "jpeg", "png", "webp"]:
        raise HTTPException(400, "Kun billeder")
    filename = f"creators/{datetime.now().timestamp()}.{ext}"
    with open(f"uploads/{filename}", "wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"url": f"http://localhost:8000/uploads/{filename}", "filename": filename}

# ── Creators ──────────────────────────────────────────────────────────────────
@router.get("/api/creators")
def get_creators():
    return {"creators": store.get("creators", [])}

@router.post("/api/creators")
def save_creator(c: Creator):
    if "creators" not in store: store["creators"] = []
    creator = {
        "id": c.id or f"creator_{datetime.now().timestamp()}",
        "name": c.name, "instagram": c.instagram, "tiktok": c.tiktok,
        "facebook": c.facebook, "twitter": c.twitter, "bio": c.bio,
        "product_groups": c.product_groups or [], "images": c.images or [],
        "active": c.active if c.active is not None else True,
        "created": datetime.now().isoformat(),
    }
    existing = [x for x in store["creators"] if x["id"] == creator["id"]]
    if existing:
        store["creators"] = [creator if x["id"] == creator["id"] else x for x in store["creators"]]
    else:
        store["creators"].insert(0, creator)
    save_store()
    return {"status": "saved", "creator": creator}

@router.delete("/api/creators/{cid}")
def delete_creator(cid: str):
    store["creators"] = [c for c in store.get("creators", []) if c["id"] != cid]
    save_store()
    return {"status": "deleted"}
