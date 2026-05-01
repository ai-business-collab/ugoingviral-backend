"""
UgoingViral — Brand Kit
GET  /api/brand-kit  → current brand kit
POST /api/brand-kit  → save brand kit
"""
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store

router = APIRouter()

_DEFAULTS = {
    "logo_url": "",
    "primary_color": "#00e5ff",
    "secondary_color": "#a855f7",
    "font": "Inter",
    "tagline": "",
}

ALLOWED = set(_DEFAULTS)


@router.get("/api/brand-kit")
def get_brand_kit(current_user: dict = Depends(get_current_user)):
    return {**_DEFAULTS, **store.get("brand_kit", {})}


@router.post("/api/brand-kit")
async def save_brand_kit(req: Request, current_user: dict = Depends(get_current_user)):
    body = await req.json()
    kit = {**_DEFAULTS, **store.get("brand_kit", {})}
    for k, v in body.items():
        if k in ALLOWED:
            kit[k] = str(v)[:500]
    store["brand_kit"] = kit
    save_store()
    return {"ok": True, "brand_kit": kit}
