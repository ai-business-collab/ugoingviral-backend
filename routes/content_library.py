"""
Content library — UgoingViral
=============================
Persists every piece of AI-generated content (videos + images) to the user's
local content directory and to a unified store entry, so the Content Library
UI can list it forever (independent of the upstream provider's URL lifetime).

The library is organised into three folders:
- videos/  → AI videos (Quick Video, Hyper Realistic, Premium Animation, etc.)
- images/  → AI images, uploaded references, generated thumbnails
- posts/   → scheduled / published posts (mirrors the existing post log)

Files live under: /root/ugoingviral-backend/user_content/{user_id}/{kind}/
and are exposed via /user_content/... so the frontend can stream them.
"""

import asyncio
import logging
import mimetypes
import os
import re
import shutil
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File
from fastapi.responses import FileResponse

from routes.auth import get_current_user
from services.store import _load_user_store, _save_user_store, store, save_store

logger = logging.getLogger("ugv.content_library")
router = APIRouter()

# Filesystem layout — files written here are also served via /user_content/...
USER_CONTENT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "user_content"
)
os.makedirs(USER_CONTENT_DIR, exist_ok=True)

# Plans that may download original files. Free plan can still browse + post.
_DOWNLOAD_PLANS = {"starter", "basic", "pro", "elite", "personal", "agency", "growth"}

# User uploads — supported formats mapped to library folder, and the size cap.
UPLOAD_EXT_KIND = {
    "mp4": "videos", "mov": "videos",
    "mp3": "audio", "wav": "audio", "aac": "audio",
    "jpg": "images", "jpeg": "images", "png": "images", "gif": "images", "webp": "images",
}
MAX_UPLOAD_BYTES = 500 * 1024 * 1024  # 500 MB per file


def _kind_dir(user_id: str, kind: str) -> str:
    """Return (and create) the per-user directory for a content kind."""
    safe = re.sub(r"[^a-z0-9_-]", "", (kind or "").lower()) or "videos"
    path = os.path.join(USER_CONTENT_DIR, user_id, safe)
    os.makedirs(path, exist_ok=True)
    return path


def _ext_from_url(url: str, default: str = ".mp4") -> str:
    try:
        path = url.split("?", 1)[0].split("#", 1)[0]
        ext = os.path.splitext(path)[1].lower()
        if ext and len(ext) <= 6:
            return ext
    except Exception:
        pass
    return default


async def _download_to_local(url: str, dest_path: str) -> bool:
    """Save the file referenced by `url` to `dest_path`.

    Accepts absolute http(s) URLs (downloaded via httpx) and locally-served
    /uploads/... paths (copied via shutil from the on-disk uploads dir).
    Returns True on success."""
    if not url:
        return False

    # Local /uploads/... or /user_content/... — copy from disk directly.
    if url.startswith("/uploads/") or url.startswith("/user_content/"):
        try:
            base = os.path.dirname(os.path.abspath(__file__))
            local_src = os.path.normpath(os.path.join(base, "..", url.lstrip("/")))
            if not os.path.exists(local_src):
                logger.warning("local source %s not found for %s", local_src, url)
                return False
            shutil.copyfile(local_src, dest_path)
            return True
        except Exception as e:
            logger.exception("local copy %s → %s failed: %s", url, dest_path, e)
            return False

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as c:
            async with c.stream("GET", url) as r:
                if r.status_code >= 400:
                    logger.warning("download failed %s status=%s", url, r.status_code)
                    return False
                tmp = dest_path + ".part"
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(chunk_size=1024 * 256):
                        f.write(chunk)
                os.replace(tmp, dest_path)
        return True
    except Exception as e:
        logger.exception("download_to_local %s → %s failed: %s", url, dest_path, e)
        return False


def _public_url(user_id: str, kind: str, filename: str) -> str:
    return f"/user_content/{user_id}/{kind}/{filename}"


async def add_to_library(
    user_id: str,
    *,
    kind: str,
    provider: str,
    source_url: Optional[str],
    prompt: str = "",
    duration: Optional[int] = None,
    aspect_ratio: str = "",
    credits_used: int = 0,
    extra: Optional[dict] = None,
) -> dict:
    """Save a generated asset to the user's library.

    If `source_url` is reachable, the file is downloaded into
    user_content/{user_id}/{kind}/ and the entry's `local_url` points to the
    locally-served copy. If download fails (or there's no URL yet — async
    providers return a job ID first) the entry is still recorded and can be
    completed later by storing the URL when the provider resolves it.
    """
    if not user_id:
        return {}

    kind = (kind or "videos").lower()
    item_id = uuid.uuid4().hex[:12]
    ext = _ext_from_url(source_url or "", default=".mp4" if kind == "videos" else ".jpg")
    filename = f"{item_id}{ext}"
    abs_path = os.path.join(_kind_dir(user_id, kind), filename)
    local_url = _public_url(user_id, kind, filename)

    saved_locally = False
    if source_url:
        saved_locally = await _download_to_local(source_url, abs_path)
        if not saved_locally:
            # Don't leave a bogus local_url pointing at a missing file.
            local_url = ""

    entry = {
        "id":            item_id,
        "kind":          kind,                  # "videos" | "images" | "posts"
        "provider":      provider or "",
        "prompt":        (prompt or "")[:600],
        "duration":      duration,
        "aspect_ratio":  aspect_ratio or "",
        "credits_used":  credits_used,
        "source_url":    source_url or "",
        "local_url":     local_url,
        "local_path":    abs_path if saved_locally else "",
        "filename":      filename if saved_locally else "",
        "size_bytes":    (os.path.getsize(abs_path) if saved_locally else 0),
        "saved_locally": saved_locally,
        "created_at":    datetime.utcnow().isoformat(),
    }
    if extra:
        # Caller-supplied metadata (e.g. product_id, scene count). Don't let
        # it overwrite the canonical fields above.
        for k, v in extra.items():
            entry.setdefault(k, v)

    ustore = _load_user_store(user_id)
    items = ustore.setdefault("library_items", [])
    items.insert(0, entry)
    ustore["library_items"] = items[:500]
    _save_user_store(user_id, ustore)
    return entry


def _is_paid_plan(user_id: str) -> bool:
    plan = (_load_user_store(user_id).get("billing", {}).get("plan") or "").lower()
    return plan in _DOWNLOAD_PLANS


# ── HTTP endpoints ────────────────────────────────────────────────────────────


@router.get("/api/library/content")
async def list_library_content(kind: str = "", current_user: dict = Depends(get_current_user)):
    """Return the user's saved library items.

    Optional `kind` filter — "videos" | "images" | "posts". The Posts folder
    aggregates the existing post-log entries (scheduled + published) so the
    Library can show them alongside generated media.
    """
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    items = list(ustore.get("library_items", []) or [])

    # Backfill: include legacy generated_videos rows so accounts created
    # before the library existed still see their past Studio generations.
    seen_ids = {i.get("id") for i in items}
    for v in (ustore.get("generated_videos") or []):
        if v.get("id") in seen_ids:
            continue
        items.append({
            "id":           v.get("id") or uuid.uuid4().hex[:12],
            "kind":         "videos",
            "provider":     v.get("provider") or v.get("kind") or "",
            "prompt":       (v.get("prompt") or "")[:600],
            "duration":     v.get("duration"),
            "aspect_ratio": v.get("aspect_ratio") or "",
            "credits_used": v.get("credits_used") or 0,
            "source_url":   v.get("url") or "",
            "local_url":    v.get("url") or "",
            "local_path":   "",
            "saved_locally": False,
            "created_at":   v.get("created_at") or "",
        })

    if kind == "posts" or kind == "":
        # Mirror scheduled/published posts into the library view so the UI can
        # show them under the Posts folder without duplicating storage.
        for p in (ustore.get("scheduled_posts") or []):
            items.append({
                "id":          p.get("id") or uuid.uuid4().hex[:12],
                "kind":        "posts",
                "provider":    "scheduler",
                "prompt":      (p.get("caption") or p.get("content") or "")[:600],
                "source_url":  p.get("image") or p.get("video_url") or "",
                "local_url":   p.get("image") or p.get("video_url") or "",
                "platform":    p.get("platform") or "",
                "status":      p.get("status") or "scheduled",
                "scheduled_at": p.get("scheduled_at") or "",
                "created_at":  p.get("created_at") or p.get("scheduled_at") or "",
            })

    if kind:
        items = [i for i in items if i.get("kind") == kind]

    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {
        "items":     items,
        "can_download": _is_paid_plan(uid),
        "plan":       _load_user_store(uid).get("billing", {}).get("plan", "free"),
    }


@router.get("/api/library/content/{item_id}/download")
async def download_library_item(item_id: str, current_user: dict = Depends(get_current_user)):
    """Stream the locally-saved file for an item. Gated to paid plans."""
    uid = current_user["id"]
    if not _is_paid_plan(uid):
        raise HTTPException(status_code=402, detail="Download is available on paid plans. Upgrade to download originals.")

    ustore = _load_user_store(uid)
    item = next((x for x in (ustore.get("library_items") or []) if x.get("id") == item_id), None)
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")

    local_path = item.get("local_path") or ""
    if not local_path or not os.path.exists(local_path):
        # Best-effort rescue: try downloading the source_url now if we have it.
        src = item.get("source_url") or ""
        if not src:
            raise HTTPException(status_code=410, detail="File no longer available on the server")
        kind = item.get("kind") or "videos"
        ext = _ext_from_url(src, default=".mp4" if kind == "videos" else ".jpg")
        new_filename = f"{item_id}{ext}"
        new_path = os.path.join(_kind_dir(uid, kind), new_filename)
        ok = await _download_to_local(src, new_path)
        if not ok:
            raise HTTPException(status_code=502, detail="Could not retrieve the file from the original provider")
        item["local_path"] = new_path
        item["local_url"]  = _public_url(uid, kind, new_filename)
        item["filename"]   = new_filename
        item["saved_locally"] = True
        item["size_bytes"] = os.path.getsize(new_path)
        _save_user_store(uid, ustore)
        local_path = new_path

    download_name = item.get("filename") or os.path.basename(local_path)
    media = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    return FileResponse(local_path, media_type=media, filename=download_name)


@router.delete("/api/library/content/{item_id}")
async def delete_library_item(item_id: str, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    items = ustore.get("library_items") or []
    target = next((x for x in items if x.get("id") == item_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Item not found")
    # Remove the local file if it lives inside the user's content dir.
    lp = target.get("local_path") or ""
    if lp and os.path.commonpath([USER_CONTENT_DIR, os.path.abspath(lp)]) == USER_CONTENT_DIR:
        try:
            if os.path.exists(lp):
                os.remove(lp)
        except Exception:
            pass
    ustore["library_items"] = [x for x in items if x.get("id") != item_id]
    _save_user_store(uid, ustore)
    return {"status": "deleted"}


@router.post("/api/library/upload")
async def upload_to_library(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Upload the user's own video / image / audio file.

    Supported: mp4, mov, mp3, wav, aac, jpg, png, gif, webp (max 500 MB).
    The file is streamed to user_content/{user_id}/uploads/ and registered in
    the content library automatically so it shows up alongside generated media.
    """
    uid = current_user["id"]
    orig = file.filename or "upload"
    ext = os.path.splitext(orig)[1].lower().lstrip(".")
    kind = UPLOAD_EXT_KIND.get(ext)
    if not kind:
        raise HTTPException(
            status_code=415,
            detail="Unsupported format. Allowed: mp4, mov, mp3, wav, aac, jpg, png, gif, webp",
        )

    uploads_dir = os.path.join(USER_CONTENT_DIR, uid, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)
    item_id = uuid.uuid4().hex[:12]
    save_ext = "jpg" if ext == "jpeg" else ext
    filename = f"{item_id}.{save_ext}"
    abs_path = os.path.join(uploads_dir, filename)

    # Stream to disk in chunks so a 500 MB upload never loads fully into memory,
    # and abort early if the size cap is exceeded.
    total = 0
    try:
        with open(abs_path, "wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="File too large. Max 500 MB per file.")
                out.write(chunk)
    except HTTPException:
        _safe_remove(abs_path)
        raise
    except Exception as e:
        _safe_remove(abs_path)
        raise HTTPException(status_code=500, detail=f"Upload failed: {str(e)[:160]}")

    if total == 0:
        _safe_remove(abs_path)
        raise HTTPException(status_code=400, detail="Empty file")

    local_url = _public_url(uid, "uploads", filename)
    media_type = {"videos": "video", "images": "image", "audio": "audio"}[kind]
    entry = {
        "id":            item_id,
        "kind":          kind,
        "provider":      "upload",
        "prompt":        orig[:200],
        "original_name": orig,
        "media_type":    media_type,
        "source_url":    local_url,
        "local_url":     local_url,
        "local_path":    abs_path,
        "filename":      filename,
        "size_bytes":    total,
        "size_mb":       round(total / 1024 / 1024, 1),
        "saved_locally": True,
        "is_upload":     True,
        "created_at":    datetime.utcnow().isoformat(),
    }

    ustore = _load_user_store(uid)
    items = ustore.setdefault("library_items", [])
    items.insert(0, entry)
    ustore["library_items"] = items[:500]
    _save_user_store(uid, ustore)
    return {"ok": True, "item": entry}


def _safe_remove(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ── Shopify content generation ────────────────────────────────────────────────


def _shopify_connected(user_id: str) -> bool:
    s = _load_user_store(user_id).get("settings", {}) or {}
    return bool(s.get("shopify_store") and (s.get("shopify_token") or s.get("shopify_access_token")))


@router.get("/api/library/shopify/products")
async def library_shopify_products(current_user: dict = Depends(get_current_user)):
    """List the user's Shopify products for the Content Library generator.
    Returns a simplified shape (id/title/description/image/price) so the UI
    can render a picker; falls back to the existing manual-products list if
    Shopify isn't connected."""
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    s = ustore.get("settings", {}) or {}
    if not _shopify_connected(uid):
        return {"connected": False, "products": ustore.get("manual_products", []) or []}

    raw_token = s.get("shopify_token", "") or s.get("shopify_access_token", "")
    store_url = (s.get("shopify_store") or "").replace("https://", "").replace("http://", "").strip("/")
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(
                f"https://{store_url}/admin/api/2024-01/products.json?limit=50",
                headers={"X-Shopify-Access-Token": raw_token},
            )
            r.raise_for_status()
            data = r.json().get("products", [])
        products = []
        for p in data:
            imgs = p.get("images", []) or []
            products.append({
                "id":          str(p.get("id")),
                "title":       p.get("title") or "",
                "description": (p.get("body_html") or "").replace("<p>", "").replace("</p>", "").strip()[:400],
                "image":       (imgs[0].get("src") if imgs else "") or "",
                "price":       (p.get("variants", [{}])[0].get("price") or "0"),
                "handle":      p.get("handle") or "",
            })
        return {"connected": True, "store": store_url, "products": products}
    except Exception as e:
        logger.exception("shopify list failed: %s", e)
        return {"connected": True, "products": [], "error": str(e)[:200]}


@router.post("/api/library/shopify/generate")
async def library_shopify_generate(req: Request, current_user: dict = Depends(get_current_user)):
    """Generate a video for a selected Shopify product and save it into the
    user's content library.

    Body: { product_id, kind='video'|'image', duration=5|10|15, aspect='9:16',
            extra_prompt, prompt_override }

    The caption is auto-built from the product title + shop link and stored
    on the library entry so the Direct Post flow can pre-fill it."""
    uid = current_user["id"]
    body = await req.json()

    product_id = (body.get("product_id") or "").strip()
    if not product_id:
        raise HTTPException(status_code=400, detail="product_id required")

    # Look up the product. Prefer Shopify, fall back to cached or manual.
    product = None
    ustore = _load_user_store(uid)
    s = ustore.get("settings", {}) or {}
    if _shopify_connected(uid):
        store_url = (s.get("shopify_store") or "").replace("https://", "").replace("http://", "").strip("/")
        raw_token = s.get("shopify_token", "") or s.get("shopify_access_token", "")
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                r = await c.get(
                    f"https://{store_url}/admin/api/2024-01/products/{product_id}.json",
                    headers={"X-Shopify-Access-Token": raw_token},
                )
                if r.status_code == 200:
                    p = r.json().get("product") or {}
                    imgs = p.get("images", []) or []
                    product = {
                        "id":          str(p.get("id")),
                        "title":       p.get("title") or "",
                        "description": (p.get("body_html") or "").replace("<p>", "").replace("</p>", "").strip()[:400],
                        "image":       (imgs[0].get("src") if imgs else "") or "",
                        "handle":      p.get("handle") or "",
                        "store_url":   store_url,
                    }
        except Exception as e:
            logger.warning("shopify single fetch failed: %s", e)

    if not product:
        cached = (ustore.get("shopify_products_cache") or []) + (ustore.get("manual_products") or [])
        product = next((x for x in cached if str(x.get("id")) == product_id), None)
        if not product:
            raise HTTPException(status_code=404, detail="Product not found — try refreshing your Shopify connection")

    title       = product.get("title") or "this product"
    description = product.get("description") or ""
    image_url   = product.get("image") or ""
    handle      = product.get("handle") or ""
    store_url   = product.get("store_url") or s.get("shopify_store", "").replace("https://", "").replace("http://", "").strip("/")
    product_link = f"https://{store_url}/products/{handle}" if (store_url and handle) else ""

    kind     = (body.get("kind") or "video").lower()
    duration = int(body.get("duration") or 5)
    aspect   = body.get("aspect") or "9:16"
    extra    = (body.get("extra_prompt") or "").strip()
    override = (body.get("prompt_override") or "").strip()
    prompt   = override or (
        f"Cinematic, scroll-stopping product video for '{title}'. "
        + (description[:200] + ". " if description else "")
        + "Eye-catching motion, premium lighting, vertical 9:16 framing. "
        + (extra + "." if extra else "")
    ).strip()

    # Caption auto-fill — used by the Direct Post button to pre-populate the
    # composer. We keep it short and add the product link so attribution is clear.
    caption_lines = [f"✨ {title}"]
    if description:
        caption_lines.append(description[:160])
    if product_link:
        caption_lines.append(product_link)
    caption = "\n\n".join(caption_lines)

    # Reuse the existing Premium Animation (pika) provider for the actual
    # generation — it accepts (prompt, duration, aspect_ratio) and is the
    # default for paid users. If the env key is missing we still record the
    # request so the user sees something in their library.
    from routes.studio import (
        _is_pro_plus, _charge_or_402, _validate_video_payload,
        _call_video_provider, _video_credit_cost, _extract_video_url,
        PIKA_API_KEY, PIKA_API_URL, RUNWAY_API_KEY,
    )

    provider_label = "pika"
    source_url     = ""
    raw_result     = None
    cost           = 0
    credits_left   = None
    if PIKA_API_KEY and _is_pro_plus(uid):
        try:
            _, duration, aspect, _img = _validate_video_payload({
                "prompt": prompt, "duration": duration, "aspect_ratio": aspect,
            })
            cost = _video_credit_cost(duration)
            credits_left = _charge_or_402(uid, cost)
            payload = {
                "prompt": prompt, "duration": duration, "aspect_ratio": aspect,
                "user_id": uid,
            }
            if image_url:
                payload["image_url"] = image_url
            raw_result = await _call_video_provider(
                PIKA_API_URL, PIKA_API_KEY, payload, label="Premium Animation"
            )
            source_url = _extract_video_url(raw_result) or ""
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("shopify generate via pika failed: %s", e)
            raw_result = {"error": str(e)[:200]}

    entry = await add_to_library(
        uid,
        kind="videos" if kind == "video" else "images",
        provider=provider_label,
        source_url=source_url or image_url,
        prompt=prompt,
        duration=duration,
        aspect_ratio=aspect,
        credits_used=cost,
        extra={
            "shopify_product_id": product_id,
            "product_title":      title,
            "product_link":       product_link,
            "product_image":      image_url,
            "caption":            caption,
            "raw_result":         raw_result,
        },
    )
    return {
        "status":       "ok" if entry.get("local_url") or source_url else "queued",
        "item":         entry,
        "caption":      caption,
        "credits_used": cost,
        "credits_left": credits_left,
    }
