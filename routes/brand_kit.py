"""
UgoingViral — Brand Kit
GET  /api/brand-kit        → current brand kit
POST /api/brand-kit        → save brand kit
POST /api/brand-kit/logo   → upload a logo (authed, per-user, public URL)
"""
import os
import hashlib

from fastapi import APIRouter, Depends, Request, UploadFile, File, HTTPException
from routes.auth import get_current_user
from services.store import store, save_store

router = APIRouter()

_DEFAULTS = {
    "logo_url": "",
    "primary_color": "#00e5ff",
    "secondary_color": "#a855f7",
    "font": "Inter",
    "tagline": "",
    # Branding-on-image controls. Overlay the logo as a watermark on generated
    # visuals; off by default so existing users see no surprise change.
    "logo_overlay": "off",
    "logo_position": "bottom-right",
}

ALLOWED = set(_DEFAULTS)

# Web-safe / Google fonts the pipeline + preview support. Keep in sync with the
# frontend <select> so a saved font always has a loadable webfont.
ALLOWED_FONTS = {"Inter", "Poppins", "Montserrat", "Playfair Display", "Oswald", "Raleway"}
_POSITIONS = {"bottom-right", "bottom-left", "top-right", "top-left", "center"}

# Logo upload constraints.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_USER_CONTENT = os.path.join(_ROOT, "user_content")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ugoingviral.com").rstrip("/")
_IMG_EXT = {"png", "jpg", "jpeg", "webp", "gif"}
_LOGO_MAX_BYTES = 8 * 1024 * 1024  # 8 MB


@router.get("/api/brand-kit")
def get_brand_kit(current_user: dict = Depends(get_current_user)):
    return {**_DEFAULTS, **store.get("brand_kit", {})}


@router.post("/api/brand-kit")
async def save_brand_kit(req: Request, current_user: dict = Depends(get_current_user)):
    body = await req.json()
    kit = {**_DEFAULTS, **store.get("brand_kit", {})}
    for k, v in body.items():
        if k not in ALLOWED:
            continue
        val = str(v)[:500]
        # Validate the constrained fields; ignore bad values rather than 500.
        if k == "font" and val not in ALLOWED_FONTS:
            continue
        if k == "logo_overlay":
            val = "on" if val in ("on", "true", "True", "1") else "off"
        if k == "logo_position" and val not in _POSITIONS:
            val = "bottom-right"
        kit[k] = val
    store["brand_kit"] = kit
    save_store()
    return {"ok": True, "brand_kit": kit}


@router.post("/api/brand-kit/logo")
async def upload_logo(file: UploadFile = File(...),
                      current_user: dict = Depends(get_current_user)):
    """Authenticated, per-user logo upload. Saves under the verified domain's
    user_content mount so the content pipeline (watermark + posting) can reach
    it, and returns a public URL. Replaces the old unauth /api/upload/image."""
    uid = current_user["id"]
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()
    if ext not in _IMG_EXT:
        raise HTTPException(400, "Logo must be PNG, JPG, WebP or GIF.")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Empty file.")
    if len(data) > _LOGO_MAX_BYTES:
        raise HTTPException(413, "Logo too large (max 8 MB).")

    d = os.path.join(_USER_CONTENT, uid, "brand")
    os.makedirs(d, exist_ok=True)
    fname = "logo_" + hashlib.md5(data[:4096] + str(len(data)).encode()).hexdigest()[:16] + f".{ext}"
    with open(os.path.join(d, fname), "wb") as f:
        f.write(data)
    url = f"{PUBLIC_BASE_URL}/user_content/{uid}/brand/{fname}"

    # Persist immediately so a logo is never lost if the user forgets to Save.
    kit = {**_DEFAULTS, **store.get("brand_kit", {})}
    kit["logo_url"] = url
    store["brand_kit"] = kit
    save_store()
    return {"ok": True, "url": url}
