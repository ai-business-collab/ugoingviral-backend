"""
Brand watermark compositor — overlays the user's Brand Kit logo onto a generated
image so posted visuals carry their brand. Pure-Pillow, no network unless the
logo is a remote URL (user_content logos resolve to a local file first).

Used by services.ai_media right after image generation, gated by the Brand Kit
(logo_url present AND logo_overlay enabled). Fails SAFE: any error returns the
original image bytes unchanged so a branding hiccup never blocks a post.
"""
import io
import os
import logging

import httpx

_LOGGER = logging.getLogger("branding")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_USER_CONTENT = os.path.join(_ROOT, "user_content")
_UPLOADS = os.path.join(_ROOT, "uploads")

# Logo occupies this fraction of the image width; padded from the edge by
# this fraction. Tuned to read as a brand mark, not dominate the visual.
_LOGO_W_FRAC = 0.22
_PAD_FRAC = 0.04
_LOGO_OPACITY = 0.85

_POSITIONS = {"bottom-right", "bottom-left", "top-right", "top-left", "center"}


def _resolve_local(logo_url: str) -> str:
    """Map a hosted logo URL back to a local file path when it lives under our
    own static mounts (avoids a network round-trip to ourselves)."""
    if not logo_url:
        return ""
    for marker, base in (("/user_content/", _USER_CONTENT), ("/uploads/", _UPLOADS)):
        if marker in logo_url:
            rel = logo_url.split(marker, 1)[1].split("?", 1)[0]
            p = os.path.join(base, *rel.split("/"))
            if os.path.exists(p):
                return p
    return ""


async def _load_logo_bytes(logo_url: str) -> bytes | None:
    local = _resolve_local(logo_url)
    if local:
        try:
            with open(local, "rb") as f:
                return f.read()
        except Exception as e:
            _LOGGER.warning("logo local read failed: %s", e)
    if logo_url.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(logo_url)
                if r.status_code == 200:
                    return r.content
        except Exception as e:
            _LOGGER.warning("logo fetch failed: %s", e)
    return None


async def apply_logo_watermark(image_bytes: bytes, logo_url: str,
                               position: str = "bottom-right") -> bytes:
    """Composite the brand logo onto image_bytes. Returns new PNG bytes, or the
    original bytes unchanged on any problem (fail-safe)."""
    if not image_bytes or not logo_url:
        return image_bytes
    try:
        from PIL import Image
    except Exception:
        _LOGGER.warning("Pillow unavailable; skipping watermark")
        return image_bytes

    logo_raw = await _load_logo_bytes(logo_url)
    if not logo_raw:
        return image_bytes

    try:
        base = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        logo = Image.open(io.BytesIO(logo_raw)).convert("RGBA")

        bw, bh = base.size
        target_w = max(1, int(bw * _LOGO_W_FRAC))
        scale = target_w / logo.width
        target_h = max(1, int(logo.height * scale))
        logo = logo.resize((target_w, target_h), Image.LANCZOS)

        # Apply opacity by scaling the logo's own alpha channel.
        if _LOGO_OPACITY < 1.0:
            alpha = logo.split()[3].point(lambda a: int(a * _LOGO_OPACITY))
            logo.putalpha(alpha)

        pad = int(bw * _PAD_FRAC)
        pos = position if position in _POSITIONS else "bottom-right"
        if pos == "center":
            x = (bw - target_w) // 2
            y = (bh - target_h) // 2
        else:
            vert, horiz = pos.split("-")
            x = pad if horiz == "left" else bw - target_w - pad
            y = pad if vert == "top" else bh - target_h - pad

        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        layer.paste(logo, (x, y), logo)
        out = Image.alpha_composite(base, layer).convert("RGB")

        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:
        _LOGGER.warning("watermark compositing failed (%s); using original", e)
        return image_bytes
