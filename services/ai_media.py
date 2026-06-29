"""
AI media generation — shared by Auto Pilot (Content Team) and Video Studio.

generate_ai_image(prompt, uid):
    Text → image via OpenAI gpt-image-1 (reuses OPENAI_API_KEY). Falls back to
    Replicate FLUX (flux-schnell). The image is saved under
    user_content/{uid}/ai/ on our TikTok-verified domain and a hosted URL is
    returned (so it can be pulled by platform post APIs and seeded into video).

generate_text_to_video(prompt, uid, ...):
    The full chain a no-products / no-uploads user needs: text → AI image →
    image→video (the existing Runway / Luma Ray generators in routes.studio).
    Returns a video URL.

These helpers only GENERATE + HOST. Credit charging is the caller's job
(content_team._charge_credits or studio._deduct) so cost stays visible there.
"""
import os
import base64
import hashlib
import logging

import httpx

_LOGGER = logging.getLogger("ai_media")

# Project root = parent of this services/ dir; user_content is served at
# {PUBLIC_BASE_URL}/user_content/ (same mount Studio/TikTok use).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_USER_CONTENT = os.path.join(_ROOT, "user_content")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://ugoingviral.com").rstrip("/")

# Default social aspect (portrait) for generated visuals.
_GPT_IMAGE_SIZE = "1024x1536"      # gpt-image-1 portrait
_FLUX_ASPECT = "9:16"


def ai_image_available() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("REPLICATE_API_KEY", "").strip())


def _save_ai_image(uid: str, data: bytes, ext: str = ".png") -> str:
    """Persist generated image bytes under the verified domain; return its URL."""
    d = os.path.join(_USER_CONTENT, uid or "shared", "ai")
    os.makedirs(d, exist_ok=True)
    fname = "ai_" + hashlib.md5(data[:4096] + str(len(data)).encode()).hexdigest()[:16] + ext
    with open(os.path.join(d, fname), "wb") as f:
        f.write(data)
    return f"{PUBLIC_BASE_URL}/user_content/{uid or 'shared'}/ai/{fname}"


async def _gpt_image(prompt: str) -> bytes | None:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": "gpt-image-1", "prompt": prompt[:3800],
                      "n": 1, "size": _GPT_IMAGE_SIZE},
            )
        if r.status_code != 200:
            _LOGGER.warning("gpt-image-1 %s: %s", r.status_code, (r.text or "")[:200])
            return None
        d = r.json().get("data", [{}])[0]
        b64 = d.get("b64_json")
        if b64:
            return base64.b64decode(b64)
        # Some responses return a URL instead of b64 — fetch it.
        url = d.get("url")
        if url:
            async with httpx.AsyncClient(timeout=60) as c:
                rr = await c.get(url)
                if rr.status_code == 200:
                    return rr.content
    except Exception as e:
        _LOGGER.warning("gpt-image-1 error: %s", e)
    return None


async def _flux_image(prompt: str) -> bytes | None:
    """Replicate FLUX schnell fallback (fast, cheap)."""
    key = os.getenv("REPLICATE_API_KEY", "").strip()
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(
                "https://api.replicate.com/v1/models/black-forest-labs/flux-schnell/predictions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                         "Prefer": "wait"},
                json={"input": {"prompt": prompt[:2000], "aspect_ratio": _FLUX_ASPECT,
                                "output_format": "png", "num_outputs": 1}},
            )
            if r.status_code not in (200, 201):
                _LOGGER.warning("FLUX %s: %s", r.status_code, (r.text or "")[:200])
                return None
            data = r.json()
            out = data.get("output")
            img_url = out[0] if isinstance(out, list) and out else (out if isinstance(out, str) else None)
            if not img_url:
                return None
            rr = await c.get(img_url, timeout=60)
            if rr.status_code == 200:
                return rr.content
    except Exception as e:
        _LOGGER.warning("FLUX error: %s", e)
    return None


async def _apply_brand_watermark(data: bytes, uid: str) -> bytes:
    """Overlay the user's Brand Kit logo when they've enabled it. Reads the
    brand kit by uid (context-independent) and fails safe to the original."""
    if not uid:
        return data
    try:
        from services.store import _load_user_store
        kit = (_load_user_store(uid) or {}).get("brand_kit", {}) or {}
        if kit.get("logo_overlay") == "on" and kit.get("logo_url"):
            from services.branding import apply_logo_watermark
            return await apply_logo_watermark(
                data, kit["logo_url"], kit.get("logo_position", "bottom-right"))
    except Exception as e:
        _LOGGER.warning("brand watermark skipped: %s", e)
    return data


async def generate_ai_image(prompt: str, uid: str = "", negative_prompt: str = "") -> str | None:
    """Generate a social image from text and return a hosted URL on the verified
    domain, or None if generation is unavailable/failed. gpt-image-1 first,
    Replicate FLUX fallback. `negative_prompt` is folded into the prompt text
    (these models take no true negative param) to suppress on-image text, logos
    and artifacts."""
    if not prompt or not prompt.strip():
        return None
    full = prompt.strip()
    if negative_prompt:
        full = f"{full}\n\nStrictly avoid: {negative_prompt}."
    data = await _gpt_image(full)
    if not data:
        data = await _flux_image(full)
    if not data:
        return None
    data = await _apply_brand_watermark(data, uid)
    try:
        return _save_ai_image(uid, data)
    except Exception as e:
        _LOGGER.error("save ai image failed: %s", e)
        return None


async def generate_text_to_video(prompt: str = "", uid: str = "", duration: int = 5,
                                 aspect_ratio: str = "9:16", provider: str = "",
                                 image_prompt: str = "", motion_prompt: str = "",
                                 negative_prompt: str = "", start_image_url: str = "",
                                 brand: dict = None, item: dict = None) -> dict:
    """Full chain: (image_prompt → AI image) OR own image → image→video. Returns
    {ok, video_url, image_url, provider, first_frame, message}.

    The image model gets a CONCRETE scene prompt and the video model gets a
    separate MOTION brief — never the raw hook. If a caller passes only `prompt`,
    we build both here from the uid's brand (airtight backstop: no path can send
    raw text to a model). When `start_image_url` is given (the user's own
    product/upload image) it becomes the video's first frame instead of a fresh
    AI image. Credit charging is the caller's responsibility."""
    # Backstop: build concrete image + motion prompts if the caller didn't.
    if not (image_prompt and motion_prompt):
        try:
            from services.visual_prompt import build_visual_prompts, build_brand_context, NEGATIVE_PROMPT
            b = brand
            if b is None:
                try:
                    from routes.content_team import _collect_signals
                    b = build_brand_context(_collect_signals(uid)) if uid else {}
                except Exception:
                    b = {}
            built = await build_visual_prompts(item or {"hook": prompt}, b)
            image_prompt = image_prompt or built.get("image_prompt", "")
            motion_prompt = motion_prompt or built.get("motion_prompt", "")
            negative_prompt = negative_prompt or built.get("negative_prompt", NEGATIVE_PROMPT)
        except Exception as e:
            _LOGGER.warning("visual prompt build failed, using raw prompt: %s", e)

    # First frame: the user's own image when available, else a fresh AI image
    # from the concrete scene prompt.
    first_frame = "own" if start_image_url else "ai"
    img = start_image_url
    if not img:
        img = await generate_ai_image(image_prompt or prompt, uid, negative_prompt)
    if not img:
        return {"ok": False, "message": "AI image generation unavailable (set OPENAI_API_KEY or REPLICATE_API_KEY)."}
    # The video model is directed by the MOTION brief, not the hook.
    video_brief = motion_prompt or prompt
    # Pick an image→video provider that's configured.
    from routes import studio
    prov = provider
    if prov not in studio.PROVIDERS or not studio.PROVIDERS.get(prov, {}).get("available"):
        prov = ""
        for p in ("runway", "luma_ray", "replicate", "luma"):
            if studio.PROVIDERS.get(p, {}).get("available"):
                prov = p
                break
    if not prov:
        # No video provider — at least return the image so the caller can still use it.
        return {"ok": False, "image_url": img,
                "message": "AI image created, but no video provider is configured (RUNWAY/LUMA/REPLICATE)."}
    # Retry transient video-gen failures (Runway/Luma queue hiccups) before
    # giving up — YouTube is a core platform, so don't drop a post on one blip.
    last_err = ""
    providers_try = [prov] + [p for p in ("luma_ray", "luma", "replicate", "runway")
                              if p != prov and studio.PROVIDERS.get(p, {}).get("available")]
    for attempt in range(3):
        use_prov = providers_try[min(attempt, len(providers_try) - 1)]
        try:
            video_url = await studio._generate_video(video_brief, use_prov, duration, aspect_ratio, image_url=img)
            if video_url:
                return {"ok": True, "video_url": video_url, "image_url": img,
                        "provider": use_prov, "first_frame": first_frame,
                        "attempts": attempt + 1}
            last_err = "empty result"
        except Exception as e:
            last_err = str(e)[:160]
        _LOGGER.warning("video gen attempt %d via %s failed: %s", attempt + 1, use_prov, last_err)
    return {"ok": False, "image_url": img, "provider": prov,
            "message": f"Video step failed after retries: {last_err}"}
