"""
Visual prompt builder — the single place that turns a content idea + the user's
real brand signals into MODEL-READY prompts.

Why this exists: image/video models must NEVER receive a raw marketing hook
("You're ordering your latte wrong"). A hook is copy, not a scene. Given a bare
slogan, gpt-image-1 invents generic stock imagery (often with garbled on-image
text) and Runway/Luma/Kling get no motion direction → the floaty "AI-ish" look.

This module converts ONE idea/script into:
  • image_prompt  — a concrete, photorealistic SCENE: subject + setting +
                    lighting + lens/angle + mood + color, plus a negative prompt.
  • motion_prompt — a camera/subject MOTION brief for image→video whose first
                    frame is the generated (or the user's own) image.

It uses an LLM when a key is configured and ALWAYS falls back to a deterministic
concrete builder, so a post is never blocked. Visual prompts are emitted in
English regardless of the caption language — image/video models comply best with
English direction.
"""
import os
import re
import json
import logging

import httpx

_LOGGER = logging.getLogger("visual_prompt")

# Appended to every image generation. The models don't all take a true negative
# parameter, so we also fold it into the prompt text downstream (ai_media).
NEGATIVE_PROMPT = (
    "no on-image text, no captions, no subtitles, no lettering, no watermark, "
    "no logos, no signature, no UI, no borders, no extra limbs, no distorted "
    "faces, no deformed hands"
)


def _location_from_goal(goal: str) -> str:
    """Best-effort city/area from a free-text goal, e.g.
    'grow my coffee shop in Aarhus' -> 'Aarhus' (EN 'in' / DA 'i')."""
    if not goal:
        return ""
    m = re.search(
        r"\b(?:in|i)\s+([A-ZÆØÅ][\wÆØÅæøå.\-]+(?:\s+[A-ZÆØÅ][\wÆØÅæøå.\-]+)?)",
        goal,
    )
    if not m:
        return ""
    loc = m.group(1).strip()
    # Don't mistake a trailing platform/word for a place.
    if loc.lower() in ("instagram", "tiktok", "youtube", "facebook", "twitter", "x"):
        return ""
    return loc[:60]


def build_brand_context(signals: dict = None, brief: dict = None,
                        goal: str = "", uid: str = "") -> dict:
    """Assemble the REAL brand signals a generator should honor. Pulls from the
    Content Team signals/brief and the per-user brand kit. Safe with partial
    input — every field degrades to a sensible blank."""
    signals = signals or {}
    brief = brief or {}

    niche = (brief.get("niche") or signals.get("niche") or "").strip()
    user_type = (brief.get("user_type")
                 or ("webshop" if signals.get("is_webshop") else "creator"))
    location = _location_from_goal(goal)

    tagline, colors, logo_url, font = "", "", "", ""
    logo_overlay, logo_position = False, "bottom-right"
    try:
        from services.store import store
        kit = store.get("brand_kit", {}) or {}
        tagline = (kit.get("tagline") or "").strip()
        cols = [c for c in (kit.get("primary_color"), kit.get("secondary_color")) if c]
        colors = ", ".join(cols)
        logo_url = (kit.get("logo_url") or "").strip()
        font = (kit.get("font") or "").strip()
        logo_overlay = (kit.get("logo_overlay") or "off") == "on"
        logo_position = (kit.get("logo_position") or "bottom-right")
    except Exception:
        pass

    # Top-performing themes = captions/titles of the user's best posts (real
    # signal from the Data Analyst), so visuals echo what already works.
    themes = []
    for tp in (signals.get("top_posts") or [])[:3]:
        cap = (tp.get("caption") or tp.get("title") or "").strip()
        if cap:
            themes.append(cap[:80])

    products = [p for p in (signals.get("products") or []) if p][:6]

    # The user's OWN images (products + uploads) — used as the image→video first
    # frame so the brand's real visuals lead, instead of a fresh AI guess.
    own_images = []
    for u in (signals.get("product_images") or []):
        if isinstance(u, str) and u:
            own_images.append(u)
    for m in (signals.get("uploads_media") or []):
        if isinstance(m, dict) and m.get("url") and m.get("type", "image") == "image":
            own_images.append(m["url"])

    return {
        "niche": niche or "general",
        "user_type": user_type,
        "location": location,
        "tagline": tagline,
        "colors": colors,
        "font": font,
        "logo_url": logo_url,
        "logo_overlay": logo_overlay,
        "logo_position": logo_position,
        "themes": themes,
        "products": products,
        "own_images": own_images,
    }


_SYSTEM = (
    "You are a senior commercial photographer AND an AI image-to-video prompt "
    "engineer. Convert ONE short-form social content idea into production-ready "
    "generation prompts.\n"
    "Return ONLY a JSON object: {\"image_prompt\": \"...\", \"motion_prompt\": \"...\"}\n\n"
    "IMAGE_PROMPT rules:\n"
    "- Describe a CONCRETE, photorealistic SCENE, never the marketing message.\n"
    "- Include all of: subject, setting/location, lighting, camera lens & angle, "
    "mood, and color palette.\n"
    "- Vertical 9:16 framing. Editorial, authentic, high detail.\n"
    "- NEVER include the hook/headline words, and never ask for any text, "
    "letters, captions, logos or watermarks in the image.\n\n"
    "MOTION_PROMPT rules:\n"
    "- The FIRST FRAME is the generated image; direct an image-to-video model "
    "from there.\n"
    "- Specify camera movement, subject/ambient motion, and pacing for a ~5s "
    "vertical clip. Keep motion subtle and realistic; no text overlays.\n\n"
    "Honor the brand's niche, business type, location, voice and color palette "
    "when provided. Be specific and intentional — avoid generic stock clichés."
)


def _user_msg(item: dict, brand: dict) -> str:
    hook = (item.get("hook") or item.get("idea") or item.get("caption") or "").strip()
    value = (item.get("value") or "").strip()
    lines = [
        f"Content idea / hook: {hook or '(none)'}",
    ]
    if value:
        lines.append(f"Script detail: {value[:200]}")
    lines.append(f"Niche: {brand.get('niche') or 'general'}")
    lines.append(f"Business type: {brand.get('user_type') or 'creator'}")
    if brand.get("location"):
        lines.append(f"Location: {brand['location']}")
    if brand.get("tagline"):
        lines.append(f"Brand voice / tagline: {brand['tagline']}")
    if brand.get("colors"):
        lines.append(f"Brand color palette: {brand['colors']}")
    if brand.get("products"):
        lines.append(f"Products to feature when relevant: {', '.join(brand['products'][:4])}")
    if brand.get("themes"):
        lines.append(f"Themes that already perform: {'; '.join(brand['themes'])}")
    lines.append("Platform: vertical 9:16 short-form social video.")
    return "\n".join(lines)


async def _llm_json(system: str, user: str) -> dict:
    """Call Anthropic (preferred) then OpenAI for a JSON visual-prompt object."""
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    openai_key = os.getenv("OPENAI_API_KEY", "").strip()
    text = ""
    if anthropic_key:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": anthropic_key,
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5", "max_tokens": 600,
                          "system": system,
                          "messages": [{"role": "user", "content": user}]},
                )
            if r.status_code == 200:
                text = r.json()["content"][0]["text"]
                _track("anthropic", r.json().get("usage", {}))
        except Exception as e:
            _LOGGER.warning("visual_prompt anthropic error: %s", e)
    if not text and openai_key:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}",
                             "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "max_tokens": 600,
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": user}]},
                )
            if r.status_code == 200:
                text = r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            _LOGGER.warning("visual_prompt openai error: %s", e)
    if not text:
        return {}
    s, e = text.find("{"), text.rfind("}") + 1
    if s < 0 or e <= 0:
        return {}
    try:
        return json.loads(text[s:e])
    except Exception:
        return {}


def _track(provider: str, usage: dict):
    try:
        from services import api_tracker
        tok = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
               or usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
        if tok:
            api_tracker.track(provider, "tokens", tok, feature="visual_prompt")
    except Exception:
        pass


def _fallback_visual(item: dict, brand: dict) -> dict:
    """Deterministic, CONCRETE prompts when the LLM is unavailable. Still a real
    scene + motion brief — never the raw hook."""
    niche = brand.get("niche") or "the subject"
    ut = brand.get("user_type") or "creator"
    loc = brand.get("location")
    setting = f"an authentic {niche} environment" + (f" in {loc}" if loc else "")

    if ut == "webshop":
        subject = f"the hero product for {niche}, styled on a clean tactile surface with props"
    elif ut == "local_business":
        subject = f"a warm candid moment inside a real {niche}, a person mid-action"
    else:
        subject = f"a relatable person genuinely engaged with {niche}"

    color_bit = f", subtle {brand['colors']} color accents" if brand.get("colors") else ""
    image_prompt = (
        f"Photorealistic vertical 9:16 photograph of {subject}, set in {setting}. "
        f"Soft natural window light, 35mm lens, eye-level angle, shallow depth of "
        f"field, warm inviting editorial mood{color_bit}. Candid, high detail, "
        f"true-to-life textures."
    )
    motion_prompt = (
        "First frame is the provided image. Slow, smooth cinematic push-in with "
        "gentle parallax and subtle real-world motion (steam, hands, ambient "
        "movement, soft light shift); steady, confident pacing over about 5 "
        "seconds. Vertical 9:16, no text overlays."
    )
    return {"image_prompt": image_prompt, "motion_prompt": motion_prompt,
            "negative_prompt": NEGATIVE_PROMPT, "source": "fallback"}


def _ensure_first_frame(motion: str) -> str:
    """Guarantee the motion brief tells the image→video model that its first
    frame is the supplied still — true for Runway/Luma/Kling — even if the LLM
    phrased it differently."""
    m = (motion or "").strip()
    if "first frame" not in m.lower():
        m = f"First frame is the provided image. {m}"
    if "no text" not in m.lower() and "no overlay" not in m.lower():
        m = f"{m} No text overlays."
    return m


async def build_visual_prompts(item: dict, brand: dict = None) -> dict:
    """Turn one idea/script item + brand context into
    {image_prompt, motion_prompt, negative_prompt, source}. LLM first, concrete
    deterministic fallback always available. NEVER returns the raw hook."""
    brand = brand or {}
    item = item or {}
    try:
        data = await _llm_json(_SYSTEM, _user_msg(item, brand))
    except Exception:
        data = {}
    image_prompt = (data.get("image_prompt") or "").strip()
    motion_prompt = (data.get("motion_prompt") or "").strip()
    if image_prompt and motion_prompt:
        # Guard: if the model lazily echoed the hook, fall back to a real scene.
        hook = (item.get("hook") or item.get("idea") or "").strip().lower()
        if hook and (image_prompt.lower() == hook or len(image_prompt) < 40):
            return _fallback_visual(item, brand)
        return {"image_prompt": image_prompt,
                "motion_prompt": _ensure_first_frame(motion_prompt),
                "negative_prompt": NEGATIVE_PROMPT, "source": "ai"}
    return _fallback_visual(item, brand)
