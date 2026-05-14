from fastapi import APIRouter, Depends
from pydantic import BaseModel
from routes.auth import get_current_user
from services.store import store, save_store
from services.users import update_user

router = APIRouter()


class SaveShopRequest(BaseModel):
    shopify_store: str = ""
    shopify_token: str = ""
    manual_url: str = ""


class SaveProfileRequest(BaseModel):
    name: str = ""
    company: str = ""
    niche: str = ""
    main_goal: str = ""


@router.get("/api/onboarding/status")
def onboarding_status(current_user: dict = Depends(get_current_user)):
    completed = store.get("onboarding_completed", False)
    settings = store.get("settings", {})
    api_enabled = settings.get("api_enabled", {})
    has_social = any(api_enabled.values())
    shopify = bool(settings.get("shopify_store", ""))
    has_products = bool(store.get("shopify_products_cache", []) or store.get("manual_products", []))
    has_content = bool(store.get("content_history", []))
    has_scheduled = bool(store.get("scheduled_posts", []))

    widget = []
    if not has_social:
        widget.append({"key": "social", "title": "Forbind sociale medier", "desc": "Ingen platforme forbundet endnu", "page": "connect", "icon": "🔗"})
    if shopify and not has_products:
        widget.append({"key": "products", "title": "Tilføj produkter", "desc": "Du har tilkoblet en webshop men ingen produkter endnu", "page": "products", "icon": "🛍️"})
    if not has_content:
        widget.append({"key": "content", "title": "Generer dit første content", "desc": "Ingen AI content genereret endnu", "page": "content", "icon": "✨"})
    if not has_scheduled:
        widget.append({"key": "schedule", "title": "Planlæg dit første opslag", "desc": "Ingen opslag planlagt endnu", "page": "posting", "icon": "📅"})

    name = current_user.get("name", "") or current_user.get("email", "").split("@")[0]
    return {"completed": completed, "widget": widget, "name": name}


@router.post("/api/onboarding/complete")
def complete_onboarding(current_user: dict = Depends(get_current_user)):
    store["onboarding_completed"] = True
    save_store()
    return {"ok": True}


@router.post("/api/onboarding/save_shop")
def save_shop(req: SaveShopRequest, current_user: dict = Depends(get_current_user)):
    s = store.get("settings", {})
    if req.shopify_store:
        s["shopify_store"] = req.shopify_store.strip()
        s["shopify_token"] = req.shopify_token.strip()
    elif req.manual_url:
        s["manual_shop_url"] = req.manual_url.strip()
    store["settings"] = s
    save_store()
    return {"ok": True}


@router.post("/api/onboarding/save_profile")
def save_ob_profile(req: SaveProfileRequest, current_user: dict = Depends(get_current_user)):
    updates = {}
    if req.name:
        updates["name"] = req.name.strip()
    if req.company:
        updates["company"] = req.company.strip()
    if req.main_goal:
        goal = req.main_goal.strip().lower()
        if goal in ("grow_followers", "sell_products", "build_brand", "agency"):
            updates["main_goal"] = goal
    if updates:
        update_user(current_user["id"], updates)
    if req.niche:
        s = store.get("settings", {})
        s["niche"] = req.niche
        store["settings"] = s
    save_store()
    return {"ok": True}
