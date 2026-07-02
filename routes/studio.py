"""
UgoingViral Video Studio — Multi-scene, multi-provider AI video generation + smart clip cutter.
Providers: Runway Gen-3, Luma Dream Machine, Replicate, Kling AI (Hyper Realistic),
Pika (Premium Animation), Luma Ray 3.2 (premium text/image-to-video).
"""
import os, uuid, subprocess, json, asyncio, httpx, re, shutil, logging, time as _time
from datetime import datetime, timedelta
from typing import List, Optional

logger = logging.getLogger("ugoingviral.studio")
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import FileResponse
from pydantic import BaseModel
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store
from services import video_edit as vedit
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), '..'))
from credit_costs import VIDEO_GENERATION, VIDEO_EDITING, VOICE_OVER

router = APIRouter()

RUNWAY_API_KEY    = os.getenv("RUNWAY_API_KEY", "")
# Runway model ids. Short clips (≤10s) use gen3a_turbo; longer 30s clips use the
# gen4.5 model which supports extended durations. Both are env-overridable.
RUNWAY_MODEL_SHORT = os.getenv("RUNWAY_MODEL", "gen3a_turbo")
RUNWAY_MODEL_LONG  = os.getenv("RUNWAY_MODEL_LONG", "gen4.5")
LUMA_API_KEY      = os.getenv("LUMA_API_KEY", "")
# Luma Ray 3.2 (GA) — premium text/image-to-video via the Luma Agents API.
# NOTE: Ray 3.2 lives on the AGENTS platform (agents.lumalabs.ai/v1), NOT the
# older Dream Machine API (api.lumalabs.ai/dream-machine/v1). The platform.lumalabs.ai
# keys (luma-api-…) authenticate against the agents host. Create body uses
# `type: "video"`; the finished MP4 URL is returned in `output[0].url`.
LUMA_API_BASE     = os.getenv("LUMA_API_BASE", "https://agents.lumalabs.ai/v1")
LUMA_RAY_MODEL    = os.getenv("LUMA_RAY_MODEL", "ray-3.2")
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY", "")
# Hyper Realistic + Premium Animation (Pro+ video providers). Base URLs are env-configurable
# because both providers gate their public APIs and the canonical endpoint may
# move; defaults below are the documented v1 paths as of June 2026.
#
# Higgsfield powers the "Hyper Realistic" engine via its cloud API
# (platform.higgsfield.ai). Auth is a single header carrying the key id + secret
# pair:  Authorization: Key <KEY_ID>:<KEY_SECRET>  (verified against the live API).
#
# Hyper Realistic uses Seedance (image2video/seedance) — the longest + highest-
# resolution realistic model on our key: user-selectable 3–12s at up to 1080p
# (verified against the live API; Kling there is capped at 5/10s and there is no
# Kling 3.0 / 15s model). Seedance is image-to-video, so a text prompt is first
# turned into a still via the "Soul" text-to-image model, then animated.
HIGGSFIELD_API_BASE       = os.getenv("HIGGSFIELD_API_BASE", "https://platform.higgsfield.ai")
HIGGSFIELD_API_KEY_ID     = os.getenv("HIGGSFIELD_API_KEY_ID", "")
HIGGSFIELD_API_KEY_SECRET = os.getenv("HIGGSFIELD_API_KEY_SECRET", "")
HIGGSFIELD_SEEDANCE_MODEL = os.getenv("HIGGSFIELD_SEEDANCE_MODEL", "seedance")
HIGGSFIELD_SEEDANCE_RES   = os.getenv("HIGGSFIELD_SEEDANCE_RES", "1080")     # 480 | 720 | 1080
HIGGSFIELD_SOUL_MODEL     = os.getenv("HIGGSFIELD_SOUL_MODEL", "soul")       # text-to-image still for the start frame
# Seedance accepts 3–12s; this is the Hyper Realistic engine's real max single clip.
HIGGSFIELD_MAX_CLIP_SECONDS = 12
# Kling v2.5 Turbo Pro on Replicate — the latest working Kling. Single clips top
# out at 10s (the model's duration enum is {5, 10}); a 15s request renders 10s.
KLING_MODEL             = os.getenv("KLING_MODEL", "kwaivgi/kling-v2.5-turbo-pro")
KLING_MAX_CLIP_SECONDS  = 10
PIKA_API_KEY       = os.getenv("PIKA_API_KEY", "")
PIKA_API_URL       = os.getenv("PIKA_API_URL", "https://api.pika.art/v1/generate")

# Plans that may use the premium video providers — any paid plan.
_PRO_PLUS = {"starter", "basic", "pro", "elite", "personal", "agency", "growth"}


def _is_pro_plus(user_id: str) -> bool:
    plan = (_load_user_store(user_id).get("billing", {}).get("plan") or "").lower()
    return plan in _PRO_PLUS


# ── Free trial videos ─────────────────────────────────────────────────────────
# New (Free-plan) users get 3 free AI videos (10s each, generated via Runway)
# before they need a paid plan. Usage is tracked per-user as `free_videos_used`.
_FREE_VIDEO_LIMIT = 3
# Luma Ray 3.2 on the agents.lumalabs.ai endpoint clamps every request to ~5s
# (verified: 9s and 99s both render ~5.0s), so 5 is the longest value it actually
# delivers — using it keeps the advertised length truthful and renders reliable.
_FREE_VIDEO_DURATION = 5            # seconds — locked length of each free PREMIUM video
_FREE_VIDEO_PROVIDER = "luma_ray"   # Luma Ray 3.2 (premium) — locked; no other provider/duration allowed
_FREE_VIDEO_PROVIDER_LABEL = "Luma Ray 3.2"


def _free_videos_status(user_id: str) -> dict:
    """Return the user's free-video allowance: {plan, limit, used, remaining}.
    EVERY user gets 3 free AI videos regardless of plan — eligibility is gated
    only on usage (free_videos_used < limit), never on the plan value."""
    ustore = _load_user_store(user_id)
    # `ustore.get("billing")` can be None (key present but null), so guard with
    # `or {}` before .get(); plan is informational only here.
    plan = ((ustore.get("billing") or {}).get("plan") or "free").lower()
    limit = _FREE_VIDEO_LIMIT  # all plans get the free-video allowance
    used = int(ustore.get("free_videos_used", 0) or 0)
    return {
        "plan": plan,
        "limit": limit,
        "used": used,
        "remaining": max(0, limit - used),
        "eligible": used < limit,  # only gate on usage, never on plan
        # The free allowance is LOCKED to premium Luma Ray 3.2, 15s, single scene.
        "premium": True,
        "provider": _FREE_VIDEO_PROVIDER,
        "provider_label": _FREE_VIDEO_PROVIDER_LABEL,
        "duration": _FREE_VIDEO_DURATION,
    }


def _consume_free_video(user_id: str) -> int:
    """Increment and persist the user's free-video counter; returns new count.
    Hard-capped at the limit so it can never exceed _FREE_VIDEO_LIMIT even under
    concurrent requests."""
    ustore = _load_user_store(user_id)
    used = min(_FREE_VIDEO_LIMIT, int(ustore.get("free_videos_used", 0) or 0) + 1)
    ustore["free_videos_used"] = used
    _save_user_store(user_id, ustore)
    return used


def _refund_free_video(user_id: str) -> int:
    """Give back a reserved free-video slot (used when a premium render fails, so
    a failure never costs the user one of their 3 free videos)."""
    ustore = _load_user_store(user_id)
    used = max(0, int(ustore.get("free_videos_used", 0) or 0) - 1)
    ustore["free_videos_used"] = used
    _save_user_store(user_id, ustore)
    return used


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_KEY    = os.getenv("ELEVENLABS_API_KEY", "")


def _track_replicate_video(user_id: str = "", seconds: int = 5) -> None:
    """Record a Kling/Replicate video generation for per-user cost tracking,
    like runway/luma do. Quantity is the clip length in SECONDS so the cost
    scales with Kling v2.5's per-second rate (api_tracker.COST_RATES['replicate_video']
    is $/second). user_id='' lets the tracker auto-resolve it from the request
    ContextVar."""
    try:
        from services import api_tracker
        api_tracker.track("replicate", "video", seconds, user_id=user_id, feature="video_generation")
    except Exception:
        pass


def _track_higgsfield_video(user_id: str = "") -> None:
    """Record one Higgsfield (Hyper Realistic / DoP) clip for per-user cost
    tracking. Flat per-clip rate lives in api_tracker.COST_RATES['higgsfield_video']."""
    try:
        from services import api_tracker
        api_tracker.track("higgsfield", "video", 1, user_id=user_id, feature="video_generation")
    except Exception:
        pass

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads", "studio")
os.makedirs(UPLOADS_DIR, exist_ok=True)


def _save_generated_video(uid: str, entry: dict) -> None:
    """Append a generated video to the user's unified `generated_videos` list.
    Keeps the most recent 100 entries so /api/content/videos can list past
    generations across all providers (Normal / Hyper Realistic / Premium
    Animation)."""
    if not uid:
        return
    ustore = _load_user_store(uid)
    history = ustore.setdefault("generated_videos", [])
    history.insert(0, entry)
    ustore["generated_videos"] = history[:100]
    _save_user_store(uid, ustore)


def _update_generated_video(uid: str, vid_id: str, patch: dict) -> None:
    """Patch fields on an existing generated_videos entry (used by the queue to
    fill in the URL/status once a queued render completes)."""
    if not uid or not vid_id:
        return
    ustore = _load_user_store(uid)
    history = ustore.get("generated_videos", []) or []
    for entry in history:
        if entry.get("id") == vid_id:
            entry.update(patch)
            break
    ustore["generated_videos"] = history
    _save_user_store(uid, ustore)

STUDIO_COSTS = {
    "video_5s":         40,
    "video_10s":        60,
    "video_15s":        80,
    "video_30s":        120,
    "extra_scene":      VIDEO_GENERATION["extended_per_10s"],          # 50
    "clip_cut":          5,
    "format_convert":    2,
    "product_overlay":   5,
    "voice_over":       VOICE_OVER["per_30s"],                         # 10
    "script_enhance":    3,
}

PROVIDERS = {
    "runway": {
        "name": "Runway Gen-3 Alpha",
        "tagline": "Cinematisk kvalitet",
        "quality": 5, "speed": 3, "cost_multiplier": 1.0,
        "styles": ["cinematic", "realistic", "dark", "vibrant", "retro"],
        "max_duration": 10,
        "available": bool(RUNWAY_API_KEY),
    },
    "luma": {
        "name": "Luma Images",
        "tagline": "Best for images",
        "quality": 5, "speed": 2, "cost_multiplier": 1.2,
        "styles": ["photorealistic", "cinematic", "nature", "sci-fi", "fantasy"],
        "max_duration": 5,
        "available": bool(LUMA_API_KEY),
    },
    "replicate": {
        "name": "Replicate (Kling)",
        "tagline": "Hurtig & fleksibel",
        "quality": 4, "speed": 5, "cost_multiplier": 0.8,
        "styles": ["animation", "realistic", "anime", "3d", "cartoon"],
        "max_duration": 10,
        "available": bool(REPLICATE_API_KEY),
    },
}

# ── Real single-clip duration caps (from production testing) ─────────────────
# Each engine delivers only specific single-clip lengths — e.g. Runway does
# 5/10/30s (never 15s as one clip), Luma/Pika do 5s, Kling v2.5 does 5/10s, and
# the "Hyper Realistic" engine does 5s natively + 10s via its Kling fallback.
# Anything longer than a single clip is built by the multi-scene stitcher, so
# these lists keep the UI honest and let us charge for the DELIVERED length.
_CLIP_DURATIONS = {
    "runway":     [5, 10, 30],
    "luma":       [5],
    "luma_ray":   [5],
    "replicate":  [5, 10],   # Kling v2.5 Turbo Pro
    "kling":      [5, 10],
    "higgsfield": [5, 10, 12],   # Seedance (up to 12s @1080p) · Kling fallback for failures
    "pika":       [5],
}


def _clip_durations(provider: str) -> list:
    return _CLIP_DURATIONS.get(provider, [5])


def _delivered_clip_seconds(provider: str, requested: int) -> int:
    """The single-clip length a provider will actually deliver for a requested
    duration: the largest supported option <= requested (never below its min)."""
    caps = _clip_durations(provider)
    eligible = [c for c in caps if c <= requested]
    return max(eligible) if eligible else min(caps)


ASPECT_RATIOS = {
    "16:9":  {"w": 1920, "h": 1080, "label": "Widescreen",   "icon": "🖥️"},
    "9:16":  {"w": 1080, "h": 1920, "label": "Vertical/Reel","icon": "📱"},
    "1:1":   {"w": 1080, "h": 1080, "label": "Square",       "icon": "⬛"},
    "4:5":   {"w": 1080, "h": 1350, "label": "Portrait",     "icon": "🖼️"},
}


# ── Models ─────────────────────────────────────────────────────────────────────

class Scene(BaseModel):
    prompt: str
    duration: int = 5           # 5, 10, 15, or 30 seconds (30s → Runway gen4.5)
    style_hint: str = ""        # optional extra style instruction
    transition: str = "cut"     # cut, fade, dissolve

class StudioRequest(BaseModel):
    mode: str = "single"        # single, series, animation
    scenes: List[Scene]
    provider: str = "runway"
    aspect_ratio: str = "16:9"
    voice_script: str = ""
    voice_enabled: bool = False
    product_title: str = ""
    product_description: str = ""
    enhance_prompts: bool = True
    series_style: str = ""      # overall visual style for multi-scene consistency

class ClipRequest(BaseModel):
    video_url: str
    clip_count: int = 5         # 3–10
    output_formats: List[str] = ["9:16", "16:9", "1:1"]
    add_captions: bool = False
    captions_text: str = ""


class SmartCutRequest(BaseModel):
    video_url: str
    remove_silence: bool = True     # auto-trim silent gaps (core v1 feature)
    silence_db: int = -30           # loudness threshold (dBFS) for "silence"
    min_silence: float = 0.6        # min silent duration (s) to cut
    pad: float = 0.12               # keep this much natural pause around speech
    max_duration: int = 0           # 0 = no cap; else trim result to N seconds

class EnhancePromptsRequest(BaseModel):
    scenes: List[Scene]
    series_style: str = ""
    product_title: str = ""
    product_description: str = ""
    mode: str = "single"


# ── Credit helper ──────────────────────────────────────────────────────────────

def _calc_cost(req: StudioRequest) -> int:
    """Flat per-duration pricing for Normal video: 5s=40, 10s=60, 15s=80.
    Provider does not affect base cost. Extras (extra scenes, product
    overlay, voice-over, prompt enhance) still apply additively."""
    cost = 0
    for i, scene in enumerate(req.scenes):
        dur_key = f"video_{scene.duration}s" if f"video_{scene.duration}s" in STUDIO_COSTS else "video_5s"
        base = STUDIO_COSTS[dur_key]
        if i > 0:
            base += STUDIO_COSTS["extra_scene"]
        if req.product_title or req.product_description:
            base += STUDIO_COSTS["product_overlay"]
        cost += base
    if req.enhance_prompts and len(req.scenes) > 1:
        cost += STUDIO_COSTS["script_enhance"] * len(req.scenes)
    if req.voice_enabled and req.voice_script:
        cost += STUDIO_COSTS["voice_over"]
    return cost


def _deduct(amount: int, uid: str = None, pack_scene: bool = False):
    """Deduct credits. If pack_scene=True, scene cost = 0 (covered by studio pack)."""
    from routes.billing import PLANS
    from services.store import _load_user_store, _save_user_store

    # Studio pack: scene generation is free if user has active pack with scenes remaining
    if pack_scene and uid:
        ustore = _load_user_store(uid)
        pack = ustore.get("studio_pack", {})
        used = pack.get("scenes_used", 0)
        total = pack.get("scenes_total", 0)
        if total > 0 and used < total:
            pack["scenes_used"] = used + 1
            ustore["studio_pack"] = pack
            _save_user_store(uid, ustore)
            # Still log the action but 0 credits
            log = store.setdefault("api_usage", [])
            log.append({"action": "studio_generate_pack", "credits": 0, "ts": datetime.now().isoformat()})
            store["api_usage"] = log[-200:]
            save_store()
            return store.get("billing", {}).get("credits", 0)

    from routes.billing import PLANS
    billing = store.setdefault("billing", {})
    plan_key = billing.get("plan", "free")
    max_cr = PLANS.get(plan_key, PLANS["free"])["credits"]
    current = billing.get("credits", max_cr)
    if current < amount:
        raise HTTPException(status_code=402,
            detail="Insufficient credits. Please top up to continue.")
    billing["credits"] = current - amount
    store["billing"] = billing
    log = store.setdefault("api_usage", [])
    log.append({"action": "studio_generate", "credits": amount, "ts": datetime.now().isoformat()})
    store["api_usage"] = log[-200:]
    save_store()
    return billing["credits"]


# ── Prompt enhancement via Claude ─────────────────────────────────────────────

async def _enhance_prompts(scenes: list, series_style: str, product_title: str,
                           product_description: str, mode: str) -> list:
    if not ANTHROPIC_API_KEY:
        return [s["prompt"] for s in scenes]

    scene_list = "\n".join([f"Scene {i+1}: {s['prompt']}" for i, s in enumerate(scenes)])
    product_ctx = f"\nProduct to blend in: {product_title} — {product_description}" if product_title else ""
    style_ctx = f"\nOverall series style: {series_style}" if series_style else ""

    sys_prompt = """You are a professional AI video prompt engineer.
Rewrite each scene prompt to be highly detailed, cinematic, and visually specific for AI video generation.
- Add lighting details, camera angles, motion descriptions
- Maintain visual consistency across scenes if it's a series
- If a product is provided, naturally blend it into scenes
- Keep each prompt under 150 words
- Return ONLY a JSON array of strings, one per scene"""

    user_msg = f"""Mode: {mode}{style_ctx}{product_ctx}

Original scenes:
{scene_list}

Return enhanced prompts as JSON array."""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_API_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 800,
                      "system": sys_prompt,
                      "messages": [{"role": "user", "content": user_msg}]}
            )
        if r.status_code == 200:
            text = r.json()["content"][0]["text"].strip()
            # Extract JSON array
            match = re.search(r'\[[\s\S]*\]', text)
            if match:
                return json.loads(match.group())
    except Exception:
        pass
    return [s["prompt"] for s in scenes]


# ── Runway video generation ────────────────────────────────────────────────────

async def _runway_generate(prompt: str, image_url: str, duration: int,
                           aspect_ratio: str = "16:9") -> Optional[str]:
    if not RUNWAY_API_KEY:
        return None
    # Runway gen3a_turbo/gen4.5 accept these exact ratio strings; older pixel
    # forms (720:1280, 1280:720) are now REJECTED with invalid_value. Social is
    # portrait, so non-landscape maps to 768:1280.
    ar_map = {"16:9": "1280:768", "9:16": "768:1280", "1:1": "768:1280", "4:5": "768:1280"}
    wh = ar_map.get(aspect_ratio, "768:1280")

    # Longer clips (30s) use the gen4.5 model; shorter clips use gen3a_turbo.
    is_long = duration > 15
    model = RUNWAY_MODEL_LONG if is_long else RUNWAY_MODEL_SHORT
    # gen3a_turbo caps at 10s; gen4.5 supports up to 30s.
    capped_duration = min(duration, 30) if is_long else min(duration, 10)
    # Long renders take longer — give them a roomier client timeout.
    async with httpx.AsyncClient(timeout=120 if is_long else 60) as client:
        # Create generation
        payload = {
            "promptText": prompt,
            "model": model,
            "duration": capped_duration,
            "ratio": wh,
        }
        if image_url:
            payload["promptImage"] = [{"uri": image_url, "position": "first"}]

        r = await client.post(
            "https://api.dev.runwayml.com/v1/image_to_video",
            headers={"Authorization": f"Bearer {RUNWAY_API_KEY}",
                     "X-Runway-Version": "2024-11-06",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Runway fejl: {r.status_code} {r.text[:200]}")
        task_id = r.json().get("id")
        if not task_id:
            raise HTTPException(status_code=500, detail="Runway returnerede intet task ID")

        # Poll for completion — 3 min for short clips, 6 min for 30s gen4.5.
        for _ in range(72 if is_long else 36):
            await asyncio.sleep(5)
            poll = await client.get(
                f"https://api.dev.runwayml.com/v1/tasks/{task_id}",
                headers={"Authorization": f"Bearer {RUNWAY_API_KEY}",
                         "X-Runway-Version": "2024-11-06"},
            )
            data = poll.json()
            status = data.get("status")
            if status == "SUCCEEDED":
                outputs = data.get("output", [])
                # Record real Runway usage (seconds rendered) for cost tracking.
                try:
                    from services import api_tracker
                    api_tracker.track("runway", "seconds", capped_duration, feature="video_generation")
                except Exception:
                    pass
                return outputs[0] if outputs else None
            if status in ("FAILED", "CANCELLED"):
                raise HTTPException(status_code=500, detail=f"Runway task fejlede: {data.get('failure','Ukendt')}")
    return None


# ── Luma Dream Machine generation ─────────────────────────────────────────────

async def _luma_generate(prompt: str, duration: int = 5, aspect_ratio: str = "16:9") -> Optional[str]:
    if not LUMA_API_KEY:
        return None
    ar_map = {"16:9": "16:9", "9:16": "9:16", "1:1": "1:1", "4:5": "4:5"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.lumalabs.ai/dream-machine/v1/generations",
            headers={"Authorization": f"Bearer {LUMA_API_KEY}",
                     "Content-Type": "application/json"},
            json={"prompt": prompt,
                  "aspect_ratio": ar_map.get(aspect_ratio, "16:9"),
                  "duration": "5s",
                  "loop": False},
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Luma fejl: {r.status_code}")
        gen_id = r.json().get("id")
        if not gen_id:
            raise HTTPException(status_code=500, detail="Luma returnerede intet generation ID")

        for _ in range(36):
            await asyncio.sleep(5)
            poll = await client.get(
                f"https://api.lumalabs.ai/dream-machine/v1/generations/{gen_id}",
                headers={"Authorization": f"Bearer {LUMA_API_KEY}"},
            )
            data = poll.json()
            state = data.get("state")
            if state == "completed":
                assets = data.get("assets", {})
                return assets.get("video")
            if state == "failed":
                raise HTTPException(status_code=500, detail=f"Luma generation fejlede: {data.get('failure_reason','')}")
    return None


# ── Luma Ray 3.2 (GA) — premium text/image-to-video ───────────────────────────
# Luma publishes a 10 req/min + 4 concurrent-job rate limit. We enforce BOTH
# globally: a single rate gate spaces out every Luma HTTP call (create + each
# poll) so the whole process never exceeds 10/min, and a semaphore caps Luma
# generations at 4 in flight. (The render queue already caps total concurrent
# renders at MAX_CONCURRENT_VIDEOS=3, so we stay comfortably under 4.)
_LUMA_MAX_PER_MIN    = 10
_LUMA_MAX_CONCURRENT = 4
_luma_call_times: list = []                 # monotonic timestamps of recent Luma calls
_luma_rate_lock      = asyncio.Lock()
_luma_concurrency    = asyncio.Semaphore(_LUMA_MAX_CONCURRENT)


async def _luma_rate_gate() -> None:
    """Block until issuing another Luma API request keeps us within 10/min."""
    async with _luma_rate_lock:
        while True:
            now = _time.monotonic()
            cutoff = now - 60.0
            while _luma_call_times and _luma_call_times[0] < cutoff:
                _luma_call_times.pop(0)
            if len(_luma_call_times) < _LUMA_MAX_PER_MIN:
                _luma_call_times.append(now)
                return
            await asyncio.sleep(max(0.1, _luma_call_times[0] + 60.0 - now + 0.05))


def _luma_text_is_funds(text: str) -> bool:
    """Heuristic: does this Luma error look like the Luma ACCOUNT being out of
    money/quota (an ops problem) rather than a bad request?"""
    t = (text or "").lower()
    return any(s in t for s in (
        "insufficient", "not enough", "out of credit", "payment", "billing",
        "balance", "funds", "quota", "subscription", "exceeded",
    ))


# ── First-frame integrity guard ───────────────────────────────────────────────
# Image-to-video providers occasionally (or, with a wrong payload, silently)
# ignore the supplied start frame and invent a scene. For a paid "make your own
# material professional" render that is worse than an error: we'd return a
# stranger's face as a success and still charge the user. So after an
# image-to-video render we compare the clip's actual first frame to the seed and
# REFUSE the result if they don't match — the render queue then refunds.
_FIRSTFRAME_MAX_DISTANCE = float(os.getenv("LUMA_FIRSTFRAME_MAX_DISTANCE", "28"))


def _img_signature(path: str) -> bytes:
    """Tiny grayscale fingerprint (32×32) for cheap perceptual comparison."""
    from PIL import Image
    with Image.open(path) as im:
        return im.convert("L").resize((32, 32)).tobytes()


def _first_frame_distance_sync(video_url: str, seed_url: str) -> Optional[float]:
    """Mean absolute per-pixel distance (0 = identical) between the video's first
    frame and the seed image. Returns None if it genuinely can't be measured so
    callers fail OPEN (never block a possibly-good render on missing infra).
    Reference points from live testing: honored start-frame ≈ 2–15, a fully
    ignored start frame ≈ 35–53."""
    import tempfile, urllib.request
    tmp = tempfile.mkdtemp(prefix="ffcheck_")
    try:
        frame = os.path.join(tmp, "f0.png")
        seed  = os.path.join(tmp, "seed.png")
        # ffmpeg reads the remote MP4 directly; grab frame 0 only.
        proc = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", video_url,
             "-vf", "select=eq(n\\,0)", "-vframes", "1", frame],
            capture_output=True, timeout=60)
        if proc.returncode != 0 or not os.path.exists(frame):
            return None
        urllib.request.urlretrieve(seed_url, seed)
        a, b = _img_signature(seed), _img_signature(frame)
        if not a or len(a) != len(b):
            return None
        return sum(abs(x - y) for x, y in zip(a, b)) / len(a)
    except Exception as e:
        logger.warning("first-frame verify skipped (infra issue): %s", e)
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _assert_first_frame_matches(video_url: str, seed_url: str, gen_id: str = "") -> None:
    """Abort (→ refund) if an image-to-video clip does not actually start from the
    user's seed image. Fails open when the check can't run."""
    dist = await asyncio.to_thread(_first_frame_distance_sync, video_url, seed_url)
    if dist is None:
        logger.info("Luma Ray %s first-frame check unavailable — allowing render", gen_id)
        return
    if dist > _FIRSTFRAME_MAX_DISTANCE:
        logger.error("Luma Ray %s IGNORED the start frame (first-frame distance "
                     "%.1f > %.1f) — discarding output and refunding the user.",
                     gen_id, dist, _FIRSTFRAME_MAX_DISTANCE)
        # 422 is deliberately OUTSIDE _is_throttle_error's retry set and carries
        # no throttle keywords, so the queue treats this as a PERMANENT failure
        # and refunds rather than burning retries re-rendering the same bad clip.
        raise HTTPException(
            status_code=422,
            detail="Video did not start from your image — discarded so you are not charged")
    logger.info("Luma Ray %s first-frame check OK (distance %.1f)", gen_id, dist)


async def _luma_ray_generate(prompt: str, duration: int = 5, aspect_ratio: str = "16:9",
                             image_url: str = "") -> Optional[str]:
    """Generate a clip with Luma Ray 3.2. Text-to-video by default; image-to-video
    when `image_url` is supplied (used as the first keyframe). Submits the
    generation, polls until completed, and returns the playable MP4 URL.

    Respects Luma's 10 req/min + 4 concurrent limits via the shared gate +
    semaphore. Raises HTTPException on failure; AUTH failures and Luma-ACCOUNT
    insufficient-funds are logged with detail and raised as non-retryable errors
    so the render queue refunds the user's credits and shows a friendly message
    (rather than burning retries against a problem only Michael can fix)."""
    if not LUMA_API_KEY:
        raise HTTPException(status_code=503, detail="Luma Ray not configured (LUMA_API_KEY missing)")
    # Luma supports 1:1, 16:9, 9:16, 4:3, 3:4, 21:9, 9:21 — map our 4:5 to 4:3.
    ar_map = {"16:9": "16:9", "9:16": "9:16", "1:1": "1:1", "4:5": "4:3"}
    payload = {
        "model":        LUMA_RAY_MODEL,
        "type":         "video",
        "prompt":       prompt,
        "aspect_ratio": ar_map.get(aspect_ratio, "16:9"),
        "duration":     f"{duration}s",
        "resolution":   os.getenv("LUMA_RAY_RESOLUTION", "720p"),
    }
    if image_url:
        # Image-to-video on the AGENTS host: the first frame MUST be supplied as
        # video.start_frame.url, with the render params nested under `video`.
        # The Dream Machine `keyframes.frame0` shape is silently DROPPED here —
        # the agents endpoint accepts unknown keys (verified: it 201s on a bogus
        # field) and falls back to text-to-video, inventing a random scene and
        # discarding the user's uploaded image. Verified live: with start_frame
        # the rendered clip opens on the seed image; with keyframes it did not.
        payload["video"] = {
            "resolution":  payload["resolution"],
            "duration":    payload["duration"],
            "start_frame": {"url": image_url},
        }
    headers = {"Authorization": f"Bearer {LUMA_API_KEY}", "Content-Type": "application/json"}

    async with _luma_concurrency:
        async with httpx.AsyncClient(timeout=60) as client:
            await _luma_rate_gate()
            r = await client.post(f"{LUMA_API_BASE}/generations", headers=headers, json=payload)
            # Auth + funds are NON-retryable (status not in the queue's throttle
            # set, detail free of throttle keywords) so the job fails fast and
            # refunds rather than burning 6 retries on a problem only Michael can
            # fix. Real 5xx fall through and DO get retried by the queue.
            if r.status_code in (401, 403):
                logger.error("Luma Ray AUTH FAILED (HTTP %s) — LUMA_API_KEY rejected by Luma. "
                             "Response: %s", r.status_code, r.text[:200])
                raise HTTPException(status_code=401, detail="Luma authentication failed")
            if r.status_code == 402 or (r.status_code >= 400 and _luma_text_is_funds(r.text)):
                logger.error("Luma Ray INSUFFICIENT FUNDS / quota on the Luma ACCOUNT — top up at "
                             "lumalabs.ai. HTTP %s response: %s", r.status_code, r.text[:300])
                raise HTTPException(status_code=402, detail="Luma account is out of credits")
            if r.status_code not in (200, 201):
                logger.warning("Luma Ray create failed HTTP %s: %s", r.status_code, r.text[:300])
                # Pass the provider status through: 4xx → permanent (bad request),
                # 5xx → the queue treats it as throttle and retries.
                raise HTTPException(status_code=r.status_code, detail=f"Luma Ray error {r.status_code}: {r.text[:160]}")
            gen_id = (r.json() or {}).get("id")
            if not gen_id:
                raise HTTPException(status_code=502, detail="Luma Ray returned no generation ID")
            logger.info("Luma Ray generation %s submitted (model=%s, %ss, %s)",
                        gen_id, LUMA_RAY_MODEL, duration, payload["aspect_ratio"])

            # Poll until completed. Ray clips render in ~1–4 min; 8s spacing keeps
            # us well within 10/min even with several jobs polling concurrently.
            for _ in range(48):
                await asyncio.sleep(8)
                await _luma_rate_gate()
                poll = await client.get(f"{LUMA_API_BASE}/generations/{gen_id}", headers=headers)
                if poll.status_code != 200:
                    logger.info("Luma Ray poll %s: HTTP %s", gen_id, poll.status_code)
                    continue
                data  = poll.json()
                state = data.get("state")
                if state == "completed":
                    # Agents API returns finished assets in output[] as
                    # {type:"video", url:"…mp4"}. Fall back to the legacy
                    # assets.video shape in case of API variance.
                    video = None
                    for item in (data.get("output") or []):
                        if isinstance(item, dict) and item.get("type") == "video" and item.get("url"):
                            video = item["url"]
                            break
                    if not video:
                        video = (data.get("assets") or {}).get("video")
                    if not video:
                        raise HTTPException(status_code=502, detail="Luma Ray completed but returned no video URL")
                    # Integrity guard: when we seeded a start frame, make sure the
                    # clip actually opens on it (catches any silent fall-back to
                    # text-to-video). Raises → queue refunds; never charges for a
                    # video that ignored the user's image.
                    if image_url:
                        await _assert_first_frame_matches(video, image_url, gen_id)
                    try:
                        from services import api_tracker
                        api_tracker.track("luma", "seconds", duration, feature="video_generation")
                    except Exception:
                        pass
                    logger.info("Luma Ray generation %s completed", gen_id)
                    return video
                if state == "failed":
                    reason = str(data.get("failure_reason") or data.get("failure_code") or "unknown")
                    if _luma_text_is_funds(reason):
                        logger.error("Luma Ray generation %s failed — Luma account funds/quota: %s",
                                     gen_id, reason)
                        raise HTTPException(status_code=402, detail="Luma account is out of credits")
                    logger.warning("Luma Ray generation %s failed: %s", gen_id, reason)
                    raise HTTPException(status_code=502, detail=f"Luma Ray failed: {reason[:160]}")
    raise HTTPException(status_code=504, detail="Luma Ray timed out — please try again")


# ── Replicate (Kling / CogVideoX) ─────────────────────────────────────────────

async def _replicate_generate(prompt: str, duration: int = 5, aspect_ratio: str = "16:9",
                              user_id: str = "") -> Optional[str]:
    if not REPLICATE_API_KEY:
        return None
    # Use CogVideoX-5b (open source, high quality)
    ar_map = {"16:9": "wide", "9:16": "tall", "1:1": "square"}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.replicate.com/v1/models/genmoai/mochi-1/predictions",
            headers={"Authorization": f"Token {REPLICATE_API_KEY}",
                     "Content-Type": "application/json"},
            json={"input": {"prompt": prompt,
                            "num_frames": duration * 8,
                            "fps": 8}},
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=500, detail=f"Replicate fejl: {r.status_code}")
        pred_id = r.json().get("id")
        if not pred_id:
            raise HTTPException(status_code=500, detail="Replicate returnerede intet prediction ID")

        for _ in range(30):
            await asyncio.sleep(6)
            poll = await client.get(
                f"https://api.replicate.com/v1/predictions/{pred_id}",
                headers={"Authorization": f"Token {REPLICATE_API_KEY}"},
            )
            data = poll.json()
            status = data.get("status")
            if status == "succeeded":
                output = data.get("output")
                _track_replicate_video(user_id, seconds=duration)
                if isinstance(output, list):
                    return output[0]
                return output
            if status == "failed":
                raise HTTPException(status_code=500, detail=f"Replicate fejlede: {data.get('error','')}")
    return None


def _kling_clip_seconds(duration: int) -> int:
    """Kling v2.5 Turbo Pro renders single clips of exactly 5s or 10s. Anything
    >= 10s (e.g. a 15s request) renders at the 10s cap; everything else is 5s."""
    return KLING_MAX_CLIP_SECONDS if duration >= KLING_MAX_CLIP_SECONDS else 5


async def _replicate_kling_generate(prompt: str, duration: int = 5,
                                    aspect_ratio: str = "16:9",
                                    image_url: str = "", user_id: str = "") -> Optional[str]:
    """Hyper Realistic video via Kling v2.5 Turbo Pro on Replicate
    (kwaivgi/kling-v2.5-turbo-pro — the latest working Kling). Submits an async
    prediction and polls until it completes, returning the playable video URL.

    Single clips cap at 10s: the model's duration is an integer enum {5, 10}, so
    a 15s request renders 10s. Image-to-video is supported via `start_image`."""
    if not REPLICATE_API_KEY:
        raise HTTPException(status_code=503, detail="Replicate API not configured (REPLICATE_API_KEY missing)")
    # Kling v2.5 accepts a limited set of aspect ratios — map anything else
    # (e.g. 4:5) to the closest supported value so the request never bounces.
    kling_aspect = aspect_ratio if aspect_ratio in ("16:9", "9:16", "1:1") else "9:16"
    clip_seconds = _kling_clip_seconds(duration)
    pred_input = {
        "prompt":       prompt,
        "duration":     clip_seconds,        # v2.5 expects an integer enum {5, 10}
        "aspect_ratio": kling_aspect,
    }
    if image_url:
        # Image-to-video reference for character consistency across a mini-series.
        # (aspect_ratio is ignored by the model when a start_image is supplied.)
        pred_input["start_image"] = image_url
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            f"https://api.replicate.com/v1/models/{KLING_MODEL}/predictions",
            headers={"Authorization": f"Token {REPLICATE_API_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "wait"},
            json={"input": pred_input},
        )
        if r.status_code not in (200, 201, 202):
            raise HTTPException(status_code=502, detail=f"Kling AI error {r.status_code}: {r.text[:200]}")
        data = r.json()
        # 200/201 with "Prefer: wait" may already be a finished prediction. A 202
        # means Replicate ACCEPTED the async prediction and it is still running —
        # that is NOT an error: fall through to the poll loop below (the response
        # still carries the prediction id). Previously 202 was raised as a hard
        # failure, so every Kling job that didn't finish within the wait window
        # was wrongly dropped.
        if data.get("status") == "succeeded":
            out = data.get("output")
            _track_replicate_video(user_id, seconds=clip_seconds)
            return out[0] if isinstance(out, list) and out else out
        pred_id = data.get("id")
        if not pred_id:
            raise HTTPException(status_code=502, detail="Kling AI returned no prediction ID")
        for _ in range(40):
            await asyncio.sleep(6)
            poll = await client.get(
                f"https://api.replicate.com/v1/predictions/{pred_id}",
                headers={"Authorization": f"Token {REPLICATE_API_KEY}"},
            )
            pdata = poll.json()
            st = pdata.get("status")
            if st == "succeeded":
                out = pdata.get("output")
                _track_replicate_video(user_id, seconds=clip_seconds)
                return out[0] if isinstance(out, list) and out else out
            if st in ("failed", "canceled"):
                raise HTTPException(status_code=502, detail=f"Kling AI generation {st}: {str(pdata.get('error',''))[:160]}")
    raise HTTPException(status_code=504, detail="Kling AI timed out — please try again")


# ── Higgsfield (Hyper Realistic) ──────────────────────────────────────────────
# Higgsfield cloud API (platform.higgsfield.ai). Auth = a single header carrying
# the key id + secret:  Authorization: Key <KEY_ID>:<KEY_SECRET>.
# Flow: POST /v1/<task>/<model> with {"params": {...}} returns a job descriptor
# {status, request_id, status_url, ...}; poll GET /requests/<request_id>/status
# until status == "completed" (or failed/nsfw). The media URL is at
# images[0].url (Soul stills) or video.url (DoP clips).
# DoP is image-to-video only, so a text prompt is first rendered to a still via
# Soul and then animated.

def _higgsfield_headers() -> dict:
    return {
        "Authorization": f"Key {HIGGSFIELD_API_KEY_ID}:{HIGGSFIELD_API_KEY_SECRET}",
        "Content-Type": "application/json",
    }


# Soul still size per aspect ratio (closest supported resolution).
_HIGGSFIELD_SOUL_SIZE = {
    "9:16": "1152x2048",
    "16:9": "2048x1152",
    "1:1":  "1536x1536",
    "4:5":  "1152x1536",
}


async def _higgsfield_run(client: httpx.AsyncClient, endpoint: str, params: dict,
                          *, poll_steps: int = 60) -> dict:
    """Submit a Higgsfield job and poll until it finishes. Returns the final
    status payload (status == 'completed'). Raises HTTPException on failure."""
    r = await client.post(
        f"{HIGGSFIELD_API_BASE}{endpoint}",
        headers=_higgsfield_headers(),
        json={"params": params},
    )
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"Hyper Realistic: {r.text[:240]}")
    data = r.json()
    status = data.get("status")
    request_id = data.get("request_id")
    if status == "completed":
        return data
    if status in ("failed", "nsfw"):
        raise HTTPException(status_code=502, detail=f"Hyper Realistic generation {status}")
    if not request_id:
        raise HTTPException(status_code=502, detail="Hyper Realistic returned no request id")
    status_path = f"/requests/{request_id}/status"
    for _ in range(poll_steps):
        await asyncio.sleep(5)
        poll = await client.get(f"{HIGGSFIELD_API_BASE}{status_path}", headers=_higgsfield_headers())
        if poll.status_code >= 500:
            continue  # transient — keep polling
        if poll.status_code >= 400:
            raise HTTPException(status_code=poll.status_code, detail=f"Hyper Realistic: {poll.text[:200]}")
        pdata = poll.json()
        st = pdata.get("status")
        if st == "completed":
            return pdata
        if st in ("failed", "nsfw"):
            raise HTTPException(status_code=502, detail=f"Hyper Realistic generation {st}")
    raise HTTPException(status_code=504, detail="Hyper Realistic timed out — please try again")


async def _higgsfield_soul_still(client: httpx.AsyncClient, prompt: str,
                                 aspect_ratio: str) -> str:
    """Render a single still from the prompt via Soul, to seed the start frame."""
    size = _HIGGSFIELD_SOUL_SIZE.get(aspect_ratio, _HIGGSFIELD_SOUL_SIZE["9:16"])
    data = await _higgsfield_run(client, "/v1/text2image/soul", {
        "prompt": prompt,
        "width_and_height": size,
        "quality": "1080p",
        "batch_size": 1,
        "enhance_prompt": True,
    })
    images = data.get("images") or []
    url = images[0].get("url") if images and isinstance(images[0], dict) else None
    if not url:
        raise HTTPException(status_code=502, detail="Hyper Realistic: no still produced")
    return url


def _seedance_clip_seconds(duration: int) -> int:
    """Seedance accepts 3–12s. Clamp the requested length to that range so the
    user gets the closest deliverable single clip (15s requests render 12s)."""
    return max(3, min(int(duration or 5), HIGGSFIELD_MAX_CLIP_SECONDS))


async def _higgsfield_generate(prompt: str, aspect_ratio: str = "9:16",
                               image_url: str = "", user_id: str = "",
                               duration: int = 5) -> Optional[str]:
    """Hyper Realistic video via Higgsfield Seedance (image-to-video, up to 12s
    @1080p, user-selectable length). When no reference image is supplied, a still
    is synthesised from the prompt via Soul first so a text prompt still works."""
    if not (HIGGSFIELD_API_KEY_ID and HIGGSFIELD_API_KEY_SECRET):
        raise HTTPException(status_code=503, detail="Higgsfield not configured (HIGGSFIELD_API_KEY_ID/SECRET missing)")
    seconds = _seedance_clip_seconds(duration)
    async with httpx.AsyncClient(timeout=90) as client:
        start_image = image_url or await _higgsfield_soul_still(client, prompt, aspect_ratio)
        data = await _higgsfield_run(client, "/v1/image2video/seedance", {
            "input_image": {"type": "image_url", "image_url": start_image},
            "prompt": prompt,
            "duration": seconds,
            "resolution": HIGGSFIELD_SEEDANCE_RES,
        }, poll_steps=90)
    video = data.get("video") or {}
    url = video.get("url") if isinstance(video, dict) else None
    if not url:
        raise HTTPException(status_code=502, detail="Hyper Realistic returned no video")
    _track_higgsfield_video(user_id)
    return url


async def _generate_video(prompt: str, provider: str, duration: int,
                          aspect_ratio: str, image_url: str = "") -> str:
    if provider == "runway":
        return await _runway_generate(prompt, image_url, duration, aspect_ratio)
    elif provider == "luma":
        return await _luma_generate(prompt, duration, aspect_ratio)
    elif provider == "luma_ray":
        # Premium Luma Ray 3.2 (used by the 3 free premium videos).
        return await _luma_ray_generate(prompt, duration, aspect_ratio, image_url)
    elif provider == "replicate":
        return await _replicate_generate(prompt, duration, aspect_ratio)
    raise HTTPException(status_code=400, detail=f"Ukendt provider: {provider}")


# ── FFmpeg helpers ─────────────────────────────────────────────────────────────

def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=5)
        return True
    except Exception:
        return False


def _download_video(url: str, dest: str) -> bool:
    try:
        import urllib.request
        urllib.request.urlretrieve(url, dest)
        return True
    except Exception:
        return False


def _get_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _concat_videos(paths: list, output: str, transitions: list = None) -> bool:
    """Concatenate multiple video files into one."""
    if not paths:
        return False
    if len(paths) == 1:
        shutil.copy(paths[0], output)
        return True
    try:
        list_file = output + ".txt"
        with open(list_file, "w") as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", output],
            capture_output=True, timeout=120
        )
        os.remove(list_file)
        return os.path.exists(output)
    except Exception:
        return False


def _crop_aspect(input_path: str, output_path: str, aspect_ratio: str) -> bool:
    """Crop/resize video to target aspect ratio."""
    ar = ASPECT_RATIOS.get(aspect_ratio, ASPECT_RATIOS["16:9"])
    w, h = ar["w"], ar["h"]
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "copy", output_path
        ], capture_output=True, timeout=120)
        return os.path.exists(output_path)
    except Exception:
        return False


def _cut_clip(input_path: str, output_path: str, start: float, duration: float) -> bool:
    """Cut a segment from a video file."""
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-ss", str(start), "-t", str(duration),
            "-c", "copy", output_path
        ], capture_output=True, timeout=60)
        return os.path.exists(output_path)
    except Exception:
        return False


# ── Voice-over via ElevenLabs ─────────────────────────────────────────────────

async def _generate_voiceover(script: str) -> Optional[str]:
    if not ELEVENLABS_KEY or not script:
        return None
    voice_id = "EXAVITQu4vr4xnSDxMaL"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={"xi-api-key": ELEVENLABS_KEY, "content-type": "application/json"},
                json={"text": script[:1000],
                      "model_id": "eleven_multilingual_v2",
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            )
        if r.status_code == 200:
            # Record real ElevenLabs usage (characters synthesised) for costs.
            try:
                from services import api_tracker
                api_tracker.track("elevenlabs", "chars", len(script[:1000]), feature="voice_over")
            except Exception:
                pass
            fn = os.path.join(UPLOADS_DIR, f"voice_{uuid.uuid4().hex[:8]}.mp3")
            with open(fn, "wb") as f:
                f.write(r.content)
            return f"/uploads/studio/{os.path.basename(fn)}"
    except Exception:
        pass
    return None




def _add_watermark(input_path: str, output_path: str) -> bool:
    """Burn 'Made with UgoingViral' text into bottom-right corner."""
    wm = (
        "drawtext=text='Made with UgoingViral'"
        ":fontcolor=white:fontsize=14"
        ":x=w-tw-12:y=h-th-12"
        ":shadowcolor=black:shadowx=1:shadowy=1"
        ":alpha=0.65"
    )
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-vf", wm,
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "copy", output_path,
        ], capture_output=True, timeout=180)
        return os.path.exists(output_path)
    except Exception:
        return False

def _merge_voiceover(video_path: str, audio_path: str, output_path: str) -> bool:
    """Mix a voice-over into a LOCAL video: VO on top, the original audio ducked
    UNDER it (sidechaincompress keyed on the VO) so narration stays clear, then
    loudnorm. Revived + corrected — the old version had no ffmpeg output mapping
    for the mixed audio and a bare amix that could drop/replace audio; it was also
    never actually called. Fails safe (returns False)."""
    try:
        fc = ("[0:a]aresample=44100[og];"
              "[1:a]aresample=44100,asplit=2[vok][vom];"
              "[og][vok]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=250[ogd];"
              "[ogd][vom]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
        r = subprocess.run([
            "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
            "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-shortest", output_path
        ], capture_output=True, timeout=180)
        return r.returncode == 0 and os.path.exists(output_path)
    except Exception:
        return False


# ── API Endpoints ──────────────────────────────────────────────────────────────

@router.get("/api/studio/providers")
def get_providers(current_user: dict = Depends(get_current_user)):
    # Attach each provider's real single-clip duration options so the UI only
    # offers lengths the provider can truly deliver (no false choices).
    providers = {k: {**v, "clip_durations": _clip_durations(k)} for k, v in PROVIDERS.items()}
    return {
        "providers": providers,
        "costs": STUDIO_COSTS,
        "aspect_ratios": ASPECT_RATIOS,
    }


@router.get("/api/studio/free_videos")
def get_free_videos(current_user: dict = Depends(get_current_user)):
    """Free-video allowance for the current user (used by onboarding + studio)."""
    status = _free_videos_status(current_user["id"])
    status["duration"] = _FREE_VIDEO_DURATION
    return status


@router.post("/api/studio/estimate")
def estimate_cost(req: StudioRequest, current_user: dict = Depends(get_current_user)):
    cost = _calc_cost(req)
    billing = store.get("billing", {})
    credits = billing.get("credits", 0)
    return {
        "total_cost": cost,
        "credits_available": credits,
        "can_afford": credits >= cost,
        "breakdown": {
            "scenes": len(req.scenes),
            "provider": req.provider,
            "voice": STUDIO_COSTS["voice_over"] if req.voice_enabled else 0,
            "enhance": STUDIO_COSTS["script_enhance"] * len(req.scenes) if req.enhance_prompts and len(req.scenes) > 1 else 0,
        }
    }


class TextToVideoRequest(BaseModel):
    prompt: str
    duration: int = 5
    aspect_ratio: str = "9:16"


def _t2v_cost(duration: int) -> int:
    """Manual text→video = AI image (8) + the Studio clip cost for the duration."""
    from routes.content_team import IMAGE_COST
    dur_key = f"video_{duration}s" if f"video_{duration}s" in STUDIO_COSTS else "video_5s"
    return IMAGE_COST + STUDIO_COSTS[dur_key]


@router.post("/api/studio/text_to_video/estimate")
def text_to_video_estimate(req: TextToVideoRequest, current_user: dict = Depends(get_current_user)):
    """Show the credit cost BEFORE generating a text→video."""
    from routes.content_team import IMAGE_COST
    cost = _t2v_cost(req.duration)
    credits = (store.get("billing", {}) or {}).get("credits", 0)
    return {"total_cost": cost, "credits_available": credits, "can_afford": credits >= cost,
            "breakdown": {"ai_image": IMAGE_COST, "video_clip": cost - IMAGE_COST}}


@router.post("/api/studio/text_to_video")
async def text_to_video(req: TextToVideoRequest, current_user: dict = Depends(get_current_user)):
    """Type a prompt → get a video via the AI-image→video chain. No product or
    upload required. Cost = AI image + clip; charged only on success."""
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="Prompt er påkrævet")
    uid = current_user["id"]
    from services.security import check_video_rate_limit, video_rate_limit_message
    if not check_video_rate_limit(uid):
        raise HTTPException(status_code=429, detail=video_rate_limit_message())

    cost = _t2v_cost(req.duration)
    credits = (_load_user_store(uid).get("billing", {}) or {}).get("credits", 0)
    if credits < cost:
        raise HTTPException(status_code=402, detail=f"Insufficient credits — need {cost}, have {credits}. Top up to continue.")

    from services.ai_media import generate_text_to_video
    duration = _delivered_clip_seconds("runway", req.duration)
    res = await generate_text_to_video(prompt, uid, duration=duration, aspect_ratio=req.aspect_ratio)
    if not res.get("ok"):
        # No charge on failure; surface the image if one was made.
        return {"ok": False, "message": res.get("message", "Generation failed"),
                "image_url": res.get("image_url", "")}

    credits_left = _deduct(cost, uid=uid)   # charge only after a real video exists
    entry = {
        "id": uuid.uuid4().hex[:10],
        "url": res["video_url"],
        "thumbnail": res.get("image_url", ""),
        "prompt": prompt,
        "provider": res.get("provider", ""),
        "kind": "text_to_video",
        "duration": duration,
        "credits_used": cost,
        "created_at": datetime.now().isoformat(),
    }
    _save_generated_video(uid, entry)
    return {"ok": True, "video_url": res["video_url"], "image_url": res.get("image_url", ""),
            "provider": res.get("provider", ""), "credits_used": cost, "credits_left": credits_left}


@router.post("/api/studio/enhance_prompts")
async def enhance_prompts(req: EnhancePromptsRequest, current_user: dict = Depends(get_current_user)):
    scenes_dicts = [{"prompt": s.prompt} for s in req.scenes]
    enhanced = await _enhance_prompts(
        scenes_dicts, req.series_style, req.product_title, req.product_description, req.mode
    )
    return {"enhanced_prompts": enhanced}


@router.post("/api/studio/generate")
async def studio_generate(req: StudioRequest, current_user: dict = Depends(get_current_user)):
    if not req.scenes:
        raise HTTPException(status_code=400, detail="Mindst én scene er påkrævet")

    # Per-user video-generation rate limit: 10 renders / hour.
    from services.security import check_video_rate_limit, video_rate_limit_message
    if not check_video_rate_limit(current_user["id"]):
        raise HTTPException(status_code=429, detail=video_rate_limit_message())

    provider = req.provider
    if provider not in PROVIDERS:
        provider = "runway"
    if not PROVIDERS[provider]["available"]:
        # Fall back to any available provider
        for p, info in PROVIDERS.items():
            if info["available"]:
                provider = p
                break
        else:
            raise HTTPException(status_code=503, detail="Ingen AI video provider er konfigureret. Tilføj RUNWAY_API_KEY, LUMA_API_KEY eller REPLICATE_API_KEY i .env")

    # Lock each scene to the length the chosen provider actually delivers as a
    # single clip, so the user is charged for what they get — never a longer
    # selected length. Longer stories are assembled from multiple scenes below.
    for s in req.scenes:
        s.duration = _delivered_clip_seconds(provider, s.duration)

    cost = _calc_cost(req)
    uid = current_user["id"]

    # ── Free trial videos ──────────────────────────────────────────────────
    # Every user gets exactly 3 FREE PREMIUM videos: Luma Ray 3.2, locked to 15s,
    # single scene. The allowance CANNOT be spent on another provider, a different
    # duration, or multiple scenes — all of that is forced server-side here so it
    # cannot be bypassed from the client. Once the 3 are used, video generation is
    # gated behind a paid plan.
    fv = _free_videos_status(uid)
    used_free_video = False
    if fv["eligible"]:
        if fv["remaining"] <= 0:
            raise HTTPException(
                status_code=403,
                detail="You've used all 3 free videos. Upgrade to a paid plan to keep generating videos.",
            )
        if not LUMA_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="Premium free videos are temporarily unavailable — please try again shortly.",
            )
        # Lock provider + duration + single scene (preserve only the user's prompt).
        provider = _FREE_VIDEO_PROVIDER                 # Luma Ray 3.2 (premium)
        first = req.scenes[0]
        first.duration = _FREE_VIDEO_DURATION           # exactly 15 seconds
        req.scenes = [first]                            # single scene only
        req.mode = "single"
        req.voice_enabled = False
        cost = 0
        used_free_video = True
        _consume_free_video(uid)                        # reserve the slot up front (hard-capped at 3)
        credits_left = (_load_user_store(uid).get("billing") or {}).get("credits", 0)
    else:
        # Check if this is pack-covered (single scene from studio pack)
        pack_scene = len(req.scenes) == 1
        credits_left = _deduct(cost, uid=uid, pack_scene=pack_scene)

    # Enhance prompts if multi-scene and enabled
    scene_prompts = [s.prompt for s in req.scenes]
    if req.enhance_prompts and (len(req.scenes) > 1 or req.product_title):
        scenes_dicts = [{"prompt": s.prompt} for s in req.scenes]
        enhanced = await _enhance_prompts(
            scenes_dicts, req.series_style,
            req.product_title, req.product_description, req.mode
        )
        scene_prompts = enhanced

    session_id = uuid.uuid4().hex[:10]
    result_videos = []

    for i, (scene, prompt) in enumerate(zip(req.scenes, scene_prompts)):
        style_prefix = f"{req.series_style} style. " if req.series_style else ""
        final_prompt = style_prefix + prompt
        if scene.style_hint:
            final_prompt += f". {scene.style_hint}"

        try:
            video_url = await _generate_video(
                final_prompt, provider,
                scene.duration, req.aspect_ratio
            )
            result_videos.append({
                "scene": i + 1,
                "prompt": prompt,
                "url": video_url,
                "duration": scene.duration,
                "status": "ok" if video_url else "failed",
            })
        except Exception as e:
            result_videos.append({
                "scene": i + 1,
                "prompt": prompt,
                "url": None,
                "duration": scene.duration,
                "status": "failed",
                "error": str(e)[:100],
            })

    # Free premium video: if the render failed, refund the reserved slot so the
    # user keeps their full allowance — a failure must never cost a free video.
    if used_free_video and not any(v.get("url") for v in result_videos):
        _refund_free_video(uid)
        raise HTTPException(
            status_code=502,
            detail="Your free premium video couldn't be generated just now — no free video was used. Please try again.",
        )

    # Assemble multi-scene into one video if all succeeded and FFmpeg is available
    assembled_url = None
    if len(result_videos) > 1 and _ffmpeg_available():
        urls = [v["url"] for v in result_videos if v["url"]]
        if len(urls) > 1:
            tmp_paths = []
            for idx, url in enumerate(urls):
                tmp = os.path.join(UPLOADS_DIR, f"tmp_{session_id}_{idx}.mp4")
                if _download_video(url, tmp):
                    tmp_paths.append(tmp)
            if tmp_paths:
                out_path = os.path.join(UPLOADS_DIR, f"series_{session_id}.mp4")
                if _concat_videos(tmp_paths, out_path,
                                  [s.transition for s in req.scenes]):
                    # Apply watermark if required
                    billing = store.get("billing", {})
                    plan = billing.get("plan", "free")
                    profile = store.get("profile", {})
                    wm_needed = (plan == "free") or profile.get("watermark_enabled", False)
                    if wm_needed and _ffmpeg_available():
                        wm_out = os.path.join(UPLOADS_DIR, f"series_{session_id}_wm.mp4")
                        if _add_watermark(out_path, wm_out):
                            os.replace(wm_out, out_path)
                    assembled_url = f"/uploads/studio/series_{session_id}.mp4"
                # Cleanup temp files
                for p in tmp_paths:
                    try: os.remove(p)
                    except: pass

    # Voice-over
    voice_url = None
    if req.voice_enabled and req.voice_script:
        voice_url = await _generate_voiceover(req.voice_script)

    # Save to history
    ustore = _load_user_store(uid)
    history = ustore.setdefault("studio_history", [])
    history.insert(0, {
        "id": session_id,
        "mode": req.mode,
        "provider": provider,
        "scenes": len(req.scenes),
        "aspect_ratio": req.aspect_ratio,
        "assembled_url": assembled_url,
        "scene_videos": result_videos,
        "voice_url": voice_url,
        "credits_used": cost,
        "created_at": datetime.now().isoformat(),
    })
    ustore["studio_history"] = history[:50]

    # Unified generated_videos list (consumed by /api/content/videos + "My
    # Videos" panel). One row per produced clip — the assembled multi-scene
    # video if present, otherwise each individual scene.
    gv = ustore.setdefault("generated_videos", [])
    now_iso = datetime.now().isoformat()
    primary_url = assembled_url or next((v["url"] for v in result_videos if v.get("url")), None)
    if primary_url:
        gv.insert(0, {
            "id":           session_id,
            "url":          primary_url,
            "provider":     provider,
            "kind":         "normal",
            "mode":         req.mode,
            "duration":     req.scenes[0].duration if req.scenes else None,
            "aspect_ratio": req.aspect_ratio,
            "prompt":       (req.scenes[0].prompt if req.scenes else "")[:240],
            "scenes":       len(req.scenes),
            "credits_used": cost,
            "created_at":   now_iso,
        })
        ustore["generated_videos"] = gv[:100]

    _save_user_store(uid, ustore)

    # Permanent library copy — download remote scene URLs and copy the
    # locally-assembled file into user_content/{uid}/videos/ so the user can
    # always find and download their generation later.
    try:
        from routes.content_library import add_to_library
        if primary_url:
            await add_to_library(
                uid, kind="videos", provider=provider,
                source_url=primary_url,
                prompt=(req.scenes[0].prompt if req.scenes else ""),
                duration=(req.scenes[0].duration if req.scenes else None),
                aspect_ratio=req.aspect_ratio,
                credits_used=cost,
                extra={"mode": req.mode, "scenes": len(req.scenes)},
            )
    except Exception:
        pass

    fv_after = _free_videos_status(uid)
    return {
        "session_id": session_id,
        "scenes": result_videos,
        "assembled_url": assembled_url,
        "voice_url": voice_url,
        "credits_used": cost,
        "credits_left": credits_left,
        "free_video_used": used_free_video,
        "free_videos_remaining": fv_after["remaining"],
        "free_videos_used": fv_after["used"],
        "free_videos_limit": fv_after["limit"],
    }


@router.post("/api/studio/clip")
async def smart_clip(req: ClipRequest, current_user: dict = Depends(get_current_user)):
    """Cut a generated video into multiple platform-optimized clips."""
    if req.clip_count < 2 or req.clip_count > 12:
        raise HTTPException(status_code=400, detail="clip_count skal være 2-12")

    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="FFmpeg er ikke installeret på serveren")

    # Calculate cost
    total_clips = req.clip_count * len(req.output_formats)
    cost = req.clip_count * 5 + (len(req.output_formats) - 1) * req.clip_count * 2
    _deduct(cost)

    session_id = uuid.uuid4().hex[:10]

    # Download source video
    src_path = os.path.join(UPLOADS_DIR, f"src_{session_id}.mp4")
    video_url = req.video_url
    if video_url.startswith("/uploads/"):
        base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads")
        src_path_local = os.path.join(base_dir, video_url[len("/uploads/"):])
        if os.path.exists(src_path_local):
            shutil.copy(src_path_local, src_path)
        else:
            raise HTTPException(status_code=404, detail="Video ikke fundet")
    elif video_url.startswith("http"):
        if not _download_video(video_url, src_path):
            raise HTTPException(status_code=500, detail="Kunne ikke downloade video")
    else:
        raise HTTPException(status_code=400, detail="Ugyldig video URL")

    total_dur = _get_duration(src_path)
    if total_dur < 1:
        raise HTTPException(status_code=400, detail="Kunne ikke læse video varighed")

    clip_dur = total_dur / req.clip_count
    output_clips = []

    for i in range(req.clip_count):
        start = i * clip_dur
        base_clip = os.path.join(UPLOADS_DIR, f"clip_{session_id}_{i}_base.mp4")
        if not _cut_clip(src_path, base_clip, start, clip_dur):
            continue

        for fmt in req.output_formats:
            if fmt not in ASPECT_RATIOS and fmt != "original":
                continue
            out_name = f"clip_{session_id}_{i}_{fmt.replace(':','x')}.mp4"
            out_path = os.path.join(UPLOADS_DIR, out_name)
            if fmt == "original":
                shutil.copy(base_clip, out_path)
                converted = True
            else:
                converted = _crop_aspect(base_clip, out_path, fmt)

            if converted:
                output_clips.append({
                    "clip_num": i + 1,
                    "format": fmt,
                    "start": round(start, 2),
                    "duration": round(clip_dur, 2),
                    "url": f"/uploads/studio/{out_name}",
                    "label": ASPECT_RATIOS.get(fmt, {}).get("label", fmt),
                    "icon": ASPECT_RATIOS.get(fmt, {}).get("icon", "🎬"),
                })
        try: os.remove(base_clip)
        except: pass

    try: os.remove(src_path)
    except: pass

    return {
        "session_id": session_id,
        "source_duration": round(total_dur, 2),
        "clip_count": req.clip_count,
        "clips": output_clips,
        "credits_used": cost,
    }


# ── Smart Cutter — auto-trim silence + tighten to the best moments ────────────
SMART_CUT_COST = 10


def _has_audio(path: str) -> bool:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=10)
        return bool(r.stdout.strip())
    except Exception:
        return False


def _detect_silence(path: str, noise_db: int, min_sil: float):
    """Return list of (silence_start, silence_end) via ffmpeg silencedetect."""
    sils, start = [], None
    try:
        r = subprocess.run(
            ["ffmpeg", "-i", path, "-af",
             f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "-"],
            capture_output=True, text=True, timeout=240)
        for line in r.stderr.splitlines():
            if "silence_start:" in line:
                try: start = float(line.split("silence_start:")[1].strip().split()[0])
                except Exception: start = None
            elif "silence_end:" in line:
                try:
                    end = float(line.split("silence_end:")[1].strip().split()[0].rstrip(","))
                    if start is not None:
                        sils.append((start, end)); start = None
                except Exception:
                    pass
    except Exception:
        pass
    return sils


def _loud_segments(total: float, sils: list, pad: float, max_segments: int = 80):
    """Invert detected silence into the speech/loud segments to keep, padded a
    little so cuts don't sound abrupt. Merges touching segments."""
    sils = sorted(sils)
    loud, cur = [], 0.0
    for s, e in sils:
        if s > cur:
            loud.append([cur, s])
        cur = max(cur, e)
    if cur < total:
        loud.append([cur, total])
    out = []
    for a, b in loud:
        a2, b2 = max(0.0, a - pad), min(total, b + pad)
        if b2 - a2 < 0.3:
            continue
        if out and a2 <= out[-1][1] + 0.05:
            out[-1][1] = max(out[-1][1], b2)
        else:
            out.append([a2, b2])
    return out[:max_segments]


def _trim_to_max(segs: list, max_duration: float):
    """Greedily keep segments from the start until the total reaches the cap."""
    if not max_duration or max_duration <= 0:
        return segs
    kept, acc = [], 0.0
    for a, b in segs:
        dur = b - a
        if acc + dur <= max_duration:
            kept.append([a, b]); acc += dur
        else:
            remain = max_duration - acc
            if remain > 0.5:
                kept.append([a, a + remain]); acc += remain
            break
    return kept or segs[:1]


def _concat_segments(src: str, segs: list, out: str, has_audio: bool) -> bool:
    """Cut `segs` out of `src` and concatenate into `out` (re-encoded)."""
    if not segs:
        return False
    fc = []
    for i, (a, b) in enumerate(segs):
        fc.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            fc.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    n = len(segs)
    joined = "".join(f"[v{i}]" + (f"[a{i}]" if has_audio else "") for i in range(n))
    if has_audio:
        fc.append(f"{joined}concat=n={n}:v=1:a=1[outv][outa]")
        maps = ["-map", "[outv]", "-map", "[outa]", "-c:a", "aac"]
    else:
        fc.append(f"{joined}concat=n={n}:v=1:a=0[outv]")
        maps = ["-map", "[outv]"]
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-filter_complex", ";".join(fc)] + maps +
            ["-c:v", "libx264", "-preset", "fast", "-crf", "23", out],
            capture_output=True, timeout=600)
        return os.path.exists(out) and os.path.getsize(out) > 0
    except Exception:
        return False


@router.post("/api/studio/smart_cut")
async def smart_cut(req: SmartCutRequest, current_user: dict = Depends(get_current_user)):
    """Smart Cutter — automatically trim a user's uploaded video to the best
    moments. v1: removes silent gaps (and optionally caps the length). The cut
    video is saved to the user's Content Library."""
    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server")

    uid = current_user["id"]
    session_id = uuid.uuid4().hex[:10]
    src_path = os.path.join(UPLOADS_DIR, f"smartcut_src_{session_id}.mp4")

    # Resolve the source video to a local file (library / uploads / http).
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    url = (req.video_url or "").strip()
    if url.startswith("/user_content/") or url.startswith("/uploads/"):
        local = os.path.normpath(os.path.join(base, url.lstrip("/")))
        if os.path.commonpath([os.path.abspath(base), local]) != os.path.abspath(base):
            raise HTTPException(status_code=400, detail="Invalid video path")
        if not os.path.exists(local):
            raise HTTPException(status_code=404, detail="Video not found")
        shutil.copy(local, src_path)
    elif url.startswith("http"):
        if not _download_video(url, src_path):
            raise HTTPException(status_code=500, detail="Could not download the video")
    else:
        raise HTTPException(status_code=400, detail="Invalid video URL")

    try:
        total = _get_duration(src_path)
        if total < 1:
            raise HTTPException(status_code=400, detail="Could not read the video duration")

        has_audio = _has_audio(src_path)
        segs, method = None, ""

        if req.remove_silence and has_audio:
            sils = _detect_silence(src_path, req.silence_db, req.min_silence)
            loud = _loud_segments(total, sils, req.pad)
            kept_total = sum(b - a for a, b in loud) if loud else 0
            # Only treat it as a real silence-cut if we actually removed something.
            if loud and kept_total < total * 0.97:
                segs, method = loud, "silence_removal"

        if segs is None:
            # Nothing to trim by silence (or no audio). If a max length was asked
            # for, just tighten to that; otherwise there's nothing to do.
            if req.max_duration and req.max_duration < total:
                segs = [[0.0, float(req.max_duration)]]
                method = "length_trim"
            else:
                raise HTTPException(
                    status_code=422,
                    detail=("No silent sections were found to trim. Try a higher silence "
                            "threshold, or set a target length."))

        # Optional hard cap on the result length.
        if req.max_duration and req.max_duration > 0:
            segs = _trim_to_max(segs, float(req.max_duration))

        _deduct(SMART_CUT_COST, uid)

        # Render straight into the Content Library's video folder.
        from routes.content_library import _kind_dir, _public_url
        item_id = uuid.uuid4().hex[:12]
        filename = f"{item_id}.mp4"
        out_path = os.path.join(_kind_dir(uid, "videos"), filename)
        if not _concat_segments(src_path, segs, out_path, has_audio):
            raise HTTPException(status_code=500, detail="Smart cut failed during rendering")

        new_dur = _get_duration(out_path)
        local_url = _public_url(uid, "videos", filename)

        # Register in the content library so it shows alongside other content.
        entry = {
            "id": item_id, "kind": "videos", "provider": "smart_cut",
            "prompt": f"Smart Cut ({method.replace('_',' ')}) — {round(total)}s → {round(new_dur)}s",
            "media_type": "video", "source_url": local_url, "local_url": local_url,
            "local_path": out_path, "filename": filename,
            "size_bytes": os.path.getsize(out_path),
            "size_mb": round(os.path.getsize(out_path) / 1024 / 1024, 1),
            "duration": round(new_dur), "saved_locally": True, "is_smart_cut": True,
            "credits_used": SMART_CUT_COST, "created_at": datetime.utcnow().isoformat(),
        }
        ustore = _load_user_store(uid)
        items = ustore.setdefault("library_items", [])
        items.insert(0, entry)
        ustore["library_items"] = items[:10000]
        # Also surface in the studio "My videos" list.
        gv = ustore.setdefault("generated_videos", [])
        gv.insert(0, {**entry, "url": local_url, "kind": "smart_cut"})
        ustore["generated_videos"] = gv[:100]
        _save_user_store(uid, ustore)

        return {
            "ok": True,
            "method": method,
            "original_duration": round(total, 1),
            "new_duration": round(new_dur, 1),
            "removed_seconds": round(max(0, total - new_dur), 1),
            "segments_kept": len(segs),
            "url": local_url,
            "credits_used": SMART_CUT_COST,
            "item": entry,
        }
    finally:
        try: os.remove(src_path)
        except Exception: pass


# ── "Make your own material professional" — Phase 1 foundation ─────────────────
# One clean, per-user pipeline that takes a user UPLOAD and processes it safely.
# Pure FFmpeg work lives in services/video_edit.py; the stateful bits (source
# resolution, Content-Library registration, credits) live here. Long renders run
# on the shared video queue (_vq) with refund-on-failure. Phases 2–4 (branding /
# captions / music) extend the step list — the plumbing is built here.
POLISH_COST   = VIDEO_EDITING["upload_and_edit"]   # 50
CAPTIONS_COST = VIDEO_EDITING["effects_filter"]    # 25
MAX_POLISH_SECONDS = 600                           # protect the box from huge re-encodes

# Bundled royalty-free music library (Phase 4). Tracks + license/attribution live
# in assets/music/manifest.json; files served via /api/studio/music/{id}/file.
_MUSIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "music")


def _music_manifest() -> dict:
    try:
        with open(os.path.join(_MUSIC_DIR, "manifest.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"tracks": []}


def _music_track(track_id: str) -> dict:
    for t in _music_manifest().get("tracks", []):
        if t.get("id") == track_id:
            return t
    return {}


def _music_path(track_id: str) -> str:
    t = _music_track(track_id)
    if not t:
        return ""
    p = os.path.join(_MUSIC_DIR, t.get("file", ""))
    return p if os.path.exists(p) else ""


def _resolve_source_video(url: str) -> str:
    """Resolve a user media URL (/user_content, /uploads, or http) to a local
    temp file. Path-traversal-safe (same guard as smart_cut). Caller deletes it."""
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    url = (url or "").strip()
    dst = os.path.join(UPLOADS_DIR, f"polsrc_{uuid.uuid4().hex[:10]}.mp4")
    if url.startswith("/user_content/") or url.startswith("/uploads/"):
        local = os.path.normpath(os.path.join(base, url.lstrip("/")))
        if os.path.commonpath([os.path.abspath(base), local]) != os.path.abspath(base):
            raise HTTPException(status_code=400, detail="Invalid video path")
        if not os.path.exists(local):
            raise HTTPException(status_code=404, detail="Video not found")
        shutil.copy(local, dst)
    elif url.startswith("http"):
        if not _download_video(url, dst):
            raise HTTPException(status_code=500, detail="Could not download the video")
    else:
        raise HTTPException(status_code=400, detail="Invalid video URL")
    return dst


def _register_polished_video(uid: str, out_path: str, *, prompt: str, provider: str,
                             cost: int, duration: float, kind_label: str) -> tuple:
    """Register a finished video into the per-user Content Library AND the studio
    'My videos' list — same shape smart_cut uses so it shows up everywhere."""
    from routes.content_library import _public_url
    filename  = os.path.basename(out_path)
    local_url = _public_url(uid, "videos", filename)
    entry = {
        "id": os.path.splitext(filename)[0], "kind": "videos", "provider": provider,
        "prompt": prompt, "media_type": "video", "source_url": local_url,
        "local_url": local_url, "local_path": out_path, "filename": filename,
        "size_bytes": os.path.getsize(out_path),
        "size_mb": round(os.path.getsize(out_path) / 1024 / 1024, 1),
        "duration": round(duration), "saved_locally": True,
        "credits_used": cost, "created_at": datetime.utcnow().isoformat(),
    }
    ustore = _load_user_store(uid)
    items = ustore.setdefault("library_items", [])
    items.insert(0, entry)
    ustore["library_items"] = items[:10000]
    gv = ustore.setdefault("generated_videos", [])
    gv.insert(0, {**entry, "url": local_url, "kind": kind_label})
    ustore["generated_videos"] = gv[:100]
    _save_user_store(uid, ustore)
    return entry, local_url


def _resolve_logo_local(logo_url: str) -> tuple:
    """Resolve a Brand Kit logo URL to a local file for the overlay input.
    Returns (path, is_temp). Prefers the on-disk file under our own mounts (no
    network); downloads remote logos to a temp file the caller must delete."""
    try:
        from services.branding import _resolve_local
        p = _resolve_local(logo_url)
        if p:
            return p, False
    except Exception:
        pass
    if (logo_url or "").startswith("http"):
        tmp = os.path.join(UPLOADS_DIR, f"logo_{uuid.uuid4().hex[:10]}")
        if _download_video(logo_url, tmp):
            return tmp, True
    return "", False


def _build_polish_layers(uid: str, steps: list) -> tuple:
    """Parse polish steps + read the user's Brand Kit into concrete render layers.
    Shared by the real render AND the preview so the two always match exactly.
    Returns (aspect, trim, logo, logo_tmp, caption). `include_logo` (from an
    'opts' step) overrides the Brand Kit's logo_overlay for this render."""
    aspect, trim, segments, include_logo, music = "9:16", None, [], None, None
    for s in (steps or []):
        op = s.get("op")
        if op == "reframe" and s.get("aspect"):
            aspect = s["aspect"]
        elif op == "trim" and float(s.get("duration") or 0) > 0:
            trim = (max(0.0, float(s.get("start") or 0)), float(s["duration"]))
        elif op == "captions":
            for role in ("hook", "body", "cta"):
                t = (s.get(role) or "").strip()
                if t:
                    pos = (s.get(f"{role}_pos") or "").strip().lower()
                    seg = {"role": role, "text": t}
                    if pos in vedit.CAPTION_POSITIONS:
                        seg["position"] = pos
                    segments.append(seg)
        elif op == "music" and s.get("track"):
            mp = _music_path(s["track"])
            if mp:
                vol = s.get("volume")
                try:
                    vol = float(vol)
                except (TypeError, ValueError):
                    vol = vedit.MUSIC_VOL_DEFAULT
                music = {"path": mp, "volume": vol, "duck": bool(s.get("duck", True))}
        elif op == "opts" and s.get("include_logo") is not None:
            include_logo = bool(s.get("include_logo"))

    kit = (_load_user_store(uid) or {}).get("brand_kit", {}) or {}
    want_logo = include_logo if include_logo is not None else (kit.get("logo_overlay") == "on")
    logo, logo_tmp = None, ""
    if want_logo and kit.get("logo_url"):
        lp, is_tmp = _resolve_logo_local(kit["logo_url"])
        if lp:
            # Auto-avoid overlap: keep the logo in a CORNER, out of any vertical
            # band a caption occupies; shrink it when a band must be shared.
            cap_bands = {(seg.get("position") or
                          vedit._CAP_DEFAULT_POS.get(seg["role"], "center")) for seg in segments}
            logo_pos = vedit.resolve_logo_position(
                kit.get("logo_position", "bottom-right"), cap_bands)
            logo = {"path": lp, "position": logo_pos}
            if logo_pos.split("-")[0] in cap_bands:
                logo["scale"] = 0.14
            if is_tmp:
                logo_tmp = lp
    caption = None
    if segments:
        caption = {
            "segments":  segments,
            "fontfile":  vedit.font_resolver(kit.get("font", "Inter")),
            "fontcolor": kit.get("primary_color", ""),
            "boxcolor":  kit.get("secondary_color", ""),
        }
    return aspect, trim, logo, logo_tmp, caption, music


def _polish_steps(req) -> list:
    """Turn a PolishRequest into the pipeline step list (shared by render + preview)."""
    steps = [{"op": "normalize"}, {"op": "reframe", "aspect": req.aspect_ratio}]
    if req.trim_duration and req.trim_duration > 0:
        steps.append({"op": "trim", "start": max(0, req.trim_start), "duration": req.trim_duration})
    body = (req.body or req.caption or "").strip()   # `caption` is a legacy alias for body
    if (req.hook or "").strip() or body or (req.cta or "").strip():
        steps.append({"op": "captions",
                      "hook": (req.hook or "").strip(), "hook_pos": req.hook_pos,
                      "body": body,                    "body_pos": req.body_pos,
                      "cta":  (req.cta or "").strip(), "cta_pos":  req.cta_pos})
    if (req.music_track or "").strip():
        steps.append({"op": "music", "track": req.music_track.strip(),
                      "volume": req.music_volume, "duck": req.music_duck})
    if req.include_logo is not None:
        steps.append({"op": "opts", "include_logo": bool(req.include_logo)})
    return steps


def run_polish_pipeline(uid: str, video_url: str, steps: list, src_path: str = "") -> dict:
    """Polish spine: resolve source → single-pass normalize+reframe(+trim)+brand
    logo overlay + brand-styled caption (via services.video_edit) → register in
    the per-user Content Library. Runs synchronously (blocking ffmpeg); the queue
    calls it via asyncio.to_thread so it never blocks the event loop. Explicit-uid
    store helpers make it safe to run without a bound request context.

    `src_path` is the already-resolved local file from the endpoint (so we don't
    download twice); if absent we resolve from `video_url`. The MAX_POLISH_SECONDS
    check here is a backstop — the endpoint rejects over-length uploads up front."""
    from routes.content_library import _kind_dir
    aspect, trim, logo, logo_tmp, caption, music = _build_polish_layers(uid, steps)
    src = src_path if (src_path and os.path.exists(src_path)) else _resolve_source_video(video_url)
    try:
        total = vedit.probe_duration(src)
        if total <= 0:
            raise HTTPException(status_code=400, detail="Could not read the video")
        if total > MAX_POLISH_SECONDS:
            raise HTTPException(status_code=400,
                                detail=f"Video too long (max {MAX_POLISH_SECONDS // 60} min)")
        item_id  = uuid.uuid4().hex[:12]
        out_path = os.path.join(_kind_dir(uid, "videos"), f"{item_id}.mp4")
        res = vedit.render_polish(src, out_path, aspect=aspect, trim=trim,
                                  logo=logo, caption=caption, music=music)
        extras = []
        if logo:    extras.append("logo")
        if caption: extras.append("caption")
        if music:   extras.append("music")
        if trim:    extras.append(f"trim {round(trim[1])}s")
        prompt = f"Polished upload → {aspect}" + (f" ({', '.join(extras)})" if extras else "")
        entry, url = _register_polished_video(
            uid, out_path, prompt=prompt, provider="polish",
            cost=POLISH_COST, duration=res.get("duration", 0), kind_label="polish")
        return {"ok": True, "url": url, "item": entry,
                "branded": bool(logo), "captioned": bool(caption),
                "width": res.get("width"), "height": res.get("height"),
                "duration": res.get("duration")}
    finally:
        try:
            os.remove(src)
        except Exception:
            pass
        if logo_tmp:
            try:
                os.remove(logo_tmp)
            except Exception:
                pass


class PolishRequest(BaseModel):
    video_url: str
    aspect_ratio: str = "9:16"
    trim_start: float = 0
    trim_duration: float = 0        # 0 = keep full length
    # Optional captions — any subset. Each renders as a timed, animated layer in
    # the user's brand color + font. All empty → no caption (clean video).
    hook: str = ""                  # opening line (timed to first ~2.5s)
    body: str = ""                  # middle line
    cta: str = ""                   # closing line (timed to last ~3s)
    caption: str = ""               # legacy alias → treated as body
    # Optional per-element placement: "top" | "center" | "bottom". Defaults:
    # hook→top, body→center, cta→bottom. The logo auto-moves to avoid overlap.
    hook_pos: str = ""
    body_pos: str = ""
    cta_pos: str = ""
    # Per-render logo toggle: True forces the Brand Kit logo on, False off; None
    # (default) follows the Brand Kit's logo_overlay setting.
    include_logo: Optional[bool] = None
    # Optional background music (id from /api/studio/music). Empty = no music
    # (original audio untouched). Ducks under speech when music_duck is on.
    music_track: str = ""
    music_volume: float = 0.35
    music_duck: bool = True


@router.post("/api/studio/polish")
async def polish_upload(req: PolishRequest, current_user: dict = Depends(get_current_user)):
    """'Make your own material professional'. Takes a user upload and runs it —
    on the video queue (refund on failure) — through normalize → reframe (→trim),
    plus their Brand Kit logo overlay (when logo_overlay is on) and an optional
    brand-styled caption. Single-pass FFmpeg; later phases add music."""
    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server")
    uid = current_user["id"]
    if req.aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(status_code=400,
                            detail=f"aspect_ratio must be one of {list(ASPECT_RATIOS)}")
    url = (req.video_url or "").strip()
    if not (url.startswith("/user_content/") or url.startswith("/uploads/") or url.startswith("http")):
        raise HTTPException(status_code=400, detail="Invalid video URL")
    # Resolve + probe the source UP FRONT so an over-length video is rejected
    # BEFORE it charges credits or takes a queue slot — one user can't tie up the
    # box with a huge render. (Resolve/probe run off-thread so we don't block the
    # loop; the resolved file is handed to the job to avoid downloading twice.)
    src_path = await asyncio.to_thread(_resolve_source_video, url)
    enqueued = False
    try:
        dur = await asyncio.to_thread(vedit.probe_duration, src_path)
        if dur <= 0:
            raise HTTPException(status_code=400, detail="Could not read the video — is it a valid file?")
        if dur > MAX_POLISH_SECONDS:
            raise HTTPException(
                status_code=400,
                detail=(f"Video is too long ({int(dur // 60)}m{int(dur % 60):02d}s). "
                        f"The max for polishing is {MAX_POLISH_SECONDS // 60} minutes — "
                        f"trim it first, then try again."))
        steps = _polish_steps(req)
        _charge_or_402(uid, POLISH_COST)      # refunded by the queue if the render fails
        resp = _enqueue_polish(uid, url, steps, POLISH_COST, req.aspect_ratio, src_path=src_path)
        enqueued = True
        return resp
    finally:
        # If we didn't hand the file off to a queued job (rejected / not enough
        # credits), delete the temp copy so nothing leaks.
        if not enqueued:
            try:
                os.remove(src_path)
            except Exception:
                pass


@router.post("/api/studio/polish/preview")
async def polish_preview(req: PolishRequest, current_user: dict = Depends(get_current_user)):
    """Render a FREE preview — one representative frame with the exact logo +
    captions the real render would burn in (all captions shown at once so the
    user sees every element + placement). NO credits charged, no queue. The user
    must see this and approve before /api/studio/polish spends any credits."""
    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server")
    uid = current_user["id"]
    if req.aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(status_code=400, detail=f"aspect_ratio must be one of {list(ASPECT_RATIOS)}")
    url = (req.video_url or "").strip()
    if not (url.startswith("/user_content/") or url.startswith("/uploads/") or url.startswith("http")):
        raise HTTPException(status_code=400, detail="Invalid video URL")
    from routes.content_library import _kind_dir, _public_url
    src_path = await asyncio.to_thread(_resolve_source_video, url)
    logo_tmp = ""
    try:
        dur = await asyncio.to_thread(vedit.probe_duration, src_path)
        if dur <= 0:
            raise HTTPException(status_code=400, detail="Could not read the video — is it a valid file?")
        if dur > MAX_POLISH_SECONDS:
            raise HTTPException(status_code=400,
                                detail=(f"Video is too long ({int(dur // 60)}m{int(dur % 60):02d}s). "
                                        f"The max for polishing is {MAX_POLISH_SECONDS // 60} minutes."))
        aspect, trim, logo, logo_tmp, caption, _music = _build_polish_layers(uid, _polish_steps(req))
        # (music isn't shown in a still preview — it doesn't affect the frame.)
        # Sample a representative frame inside the (trimmed) range.
        at = (trim[0] + min(1.0, trim[1] / 2)) if trim else min(1.0, dur / 2)
        fname = f"prev_{uuid.uuid4().hex[:12]}.jpg"
        out = os.path.join(_kind_dir(uid, "previews"), fname)
        await asyncio.to_thread(vedit.render_preview_frame, src_path, out,
                                aspect=aspect, logo=logo, caption=caption, at=at)
        return {"ok": True, "preview_url": _public_url(uid, "previews", fname),
                "cost": POLISH_COST, "branded": bool(logo), "captioned": bool(caption),
                "note": "Hook shows at the start and CTA at the end in the final video; "
                        "here they're shown together so you can check placement."}
    finally:
        for p in (src_path, logo_tmp):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


@router.get("/api/studio/music")
def list_music(current_user: dict = Depends(get_current_user)):
    """The bundled royalty-free music library the polish flow can use. Each track
    carries its license + attribution so the user can credit it."""
    m = _music_manifest()
    tracks = []
    for t in m.get("tracks", []):
        if _music_path(t.get("id", "")):
            tracks.append({
                "id": t["id"], "title": t.get("title"), "mood": t.get("mood", ""),
                "duration": t.get("duration"), "license": t.get("license"),
                "attribution": t.get("attribution"),
                "url": f"/api/studio/music/{t['id']}/file",
            })
    return {"tracks": tracks, "license_url": m.get("license_url"),
            "note": "Royalty-free (CC BY 4.0) — free for commercial use on "
                    "TikTok/YouTube/Instagram. Please credit the artist (attribution shown per track)."}


@router.get("/api/studio/music/{track_id}/file")
def get_music_file(track_id: str, current_user: dict = Depends(get_current_user)):
    """Serve a bundled track so the UI can play a preview-listen before rendering."""
    p = _music_path(track_id)
    if not p:
        raise HTTPException(status_code=404, detail="Track not found")
    return FileResponse(p, media_type="audio/mpeg")


class StudioCaptionRequest(BaseModel):
    video_url: str
    caption: str


@router.post("/api/studio/captions")
async def studio_captions(req: StudioCaptionRequest, current_user: dict = Depends(get_current_user)):
    """Burn a caption onto a user's video and save it to THEIR Content Library.
    Canonical, per-user replacement for /api/content/add_captions (which stored
    into a shared uploads/ folder + a platform-keyed pipeline). Synchronous."""
    if not _ffmpeg_available():
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server")
    uid  = current_user["id"]
    text = (req.caption or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="caption text required")
    from routes.content_library import _kind_dir
    src      = _resolve_source_video(req.video_url)
    item_id  = uuid.uuid4().hex[:12]
    out_path = os.path.join(_kind_dir(uid, "videos"), f"{item_id}.mp4")
    _charge_or_402(uid, CAPTIONS_COST)
    try:
        await asyncio.to_thread(vedit.burn_caption, src, out_path, text)
    except Exception as exc:
        _refund(uid, CAPTIONS_COST)
        raise HTTPException(status_code=500,
                            detail=f"Caption render failed: {str(getattr(exc, 'detail', exc))[:160]}")
    finally:
        try:
            os.remove(src)
        except Exception:
            pass
    dur = vedit.probe_duration(out_path)
    entry, url = _register_polished_video(
        uid, out_path, prompt=f"Captioned: {text[:60]}", provider="captions",
        cost=CAPTIONS_COST, duration=dur, kind_label="captioned")
    return {"ok": True, "url": url, "item": entry, "credits_used": CAPTIONS_COST}


@router.get("/api/studio/history")
def get_studio_history(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    return {"history": ustore.get("studio_history", [])[:30]}


@router.post("/api/studio/activate_pack")
async def activate_studio_pack(body: dict, current_user: dict = Depends(get_current_user)):
    """Activate a studio pack after purchase. Sets scenes_used=0, scenes_total=N."""
    pack_key = body.get("pack_key", "")  # scenes_3, scenes_6, scenes_10
    pack_map = {"scenes_3": 3, "scenes_6": 6, "scenes_10": 10}
    scenes = pack_map.get(pack_key)
    if not scenes:
        raise HTTPException(status_code=400, detail=f"Unknown pack: {pack_key}")
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    existing = ustore.get("studio_pack", {})
    # Add to remaining scenes instead of resetting
    ustore["studio_pack"] = {
        "pack_key": pack_key,
        "scenes_total": existing.get("scenes_total", 0) - existing.get("scenes_used", 0) + scenes,
        "scenes_used": 0,
        "activated_at": datetime.now().isoformat(),
    }
    _save_user_store(uid, ustore)
    return {"ok": True, "scenes_total": ustore["studio_pack"]["scenes_total"]}


@router.get("/api/studio/pack_status")
def studio_pack_status(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    pack = ustore.get("studio_pack", {})
    used = pack.get("scenes_used", 0)
    total = pack.get("scenes_total", 0)
    return {
        "has_pack": total > used,
        "scenes_remaining": max(0, total - used),
        "scenes_used": used,
        "scenes_total": total,
    }


# ── AI Portrait / Face-Scene ───────────────────────────────────────────────────

FACE_SCENE_COST = 50

FACE_SCENE_PRESETS = [
    {"label": "CEO Headshot",       "prompt": "CEO headshot, modern office background, professional lighting, sharp focus"},
    {"label": "Business Meeting",   "prompt": "Professional business meeting, modern office, shot from above, photorealistic"},
    {"label": "Conference Room",    "prompt": "Team meeting around conference table, 5 people, modern office"},
    {"label": "LinkedIn Profile",   "prompt": "Professional LinkedIn profile photo, corporate background, natural smile"},
    {"label": "Product Launch",     "prompt": "Product launch event stage, dramatic lighting, crowd in background"},
    {"label": "Outdoor Creative",   "prompt": "Creative outdoor portrait, nature background, golden hour lighting"},
]


class FaceSceneRequest(BaseModel):
    face_image: str          # base64 data URL (data:image/...) or public https:// URL
    scene_prompt: str
    style: str = "professional"


@router.post("/api/studio/face-scene")
async def face_scene(req: FaceSceneRequest, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]

    # Per-user video-generation rate limit: 10 renders / hour.
    from services.security import check_video_rate_limit, video_rate_limit_message
    if not check_video_rate_limit(uid):
        raise HTTPException(status_code=429, detail=video_rate_limit_message())

    if not REPLICATE_API_KEY:
        raise HTTPException(status_code=503, detail="Replicate API not configured")

    # ── Free-plan gate ────────────────────────────────────────────────────────
    from routes.billing import PLANS as _PLANS
    billing = store.get("billing", {})
    plan = billing.get("plan", "free")
    if plan == "free":
        raise HTTPException(status_code=403, detail="Upgrade required to use AI Portrait")

    # ── Check credits (don't deduct yet) ─────────────────────────────────────
    current_credits = billing.get("credits", 0)
    if current_credits < FACE_SCENE_COST:
        raise HTTPException(status_code=402,
            detail=f"Insufficient credits. Need {FACE_SCENE_COST}, have {current_credits}.")

    # ── Resolve face image to a public URL Replicate can fetch ────────────────
    face_url = req.face_image
    if face_url.startswith("data:"):
        import base64 as _b64
        try:
            header, b64data = face_url.split(",", 1)
            ext = "png" if "png" in header else "jpg"
            img_bytes = _b64.b64decode(b64data)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid base64 image")
        if len(img_bytes) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Image too large (max 10MB)")
        fname = f"face_{uuid.uuid4().hex[:8]}.{ext}"
        fpath = os.path.join(UPLOADS_DIR, fname)
        with open(fpath, "wb") as fh:
            fh.write(img_bytes)
        face_url = f"https://ugoingviral.com/uploads/studio/{fname}"

    if not face_url.startswith("http"):
        raise HTTPException(status_code=400, detail="face_image must be base64 or a public URL")

    # ── Build prompt ──────────────────────────────────────────────────────────
    style_suffix = {
        "professional": "professional lighting, sharp focus, photorealistic, 8k quality",
        "creative":     "artistic, vibrant colors, creative composition, dramatic lighting",
        "casual":       "natural lighting, candid atmosphere, lifestyle, warm tones",
    }.get(req.style, "photorealistic, high quality")
    prompt = f"{req.scene_prompt.strip()}, {style_suffix}"

    # ── Call Replicate InstantID ──────────────────────────────────────────────
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(
            "https://api.replicate.com/v1/models/zsxkib/instant-id/predictions",
            headers={
                "Authorization": f"Token {REPLICATE_API_KEY}",
                "Content-Type":  "application/json",
                "Prefer":        "wait",
            },
            json={"input": {
                "image":               face_url,
                "prompt":              prompt,
                "negative_prompt":     "ugly, blurry, low quality, deformed, bad anatomy, watermark",
                "num_inference_steps": 30,
                "guidance_scale":      5,
            }},
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=500,
                                detail=f"Replicate error {r.status_code}: {r.text[:200]}")

        data = r.json()
        pred_id = data.get("id")
        if not pred_id:
            raise HTTPException(status_code=500, detail="No prediction ID returned by Replicate")

        img_url = ""

        # If Prefer:wait succeeded synchronously, check immediately
        if data.get("status") == "succeeded":
            output = data.get("output")
            img_url = (output[0] if isinstance(output, list) else output) or ""

        if not img_url:
            # ── Poll until done ───────────────────────────────────────────────
            for _ in range(40):
                await asyncio.sleep(3)
                poll = await client.get(
                    f"https://api.replicate.com/v1/predictions/{pred_id}",
                    headers={"Authorization": f"Token {REPLICATE_API_KEY}"},
                )
                result = poll.json()
                status = result.get("status")
                if status == "succeeded":
                    output = result.get("output")
                    img_url = (output[0] if isinstance(output, list) else output) or ""
                    break
                if status == "failed":
                    raise HTTPException(status_code=500,
                                        detail=f"Generation failed: {result.get('error', 'unknown')}")

        if not img_url:
            raise HTTPException(status_code=504, detail="Portrait generation timed out")

        # ── Deduct credits only on success ────────────────────────────────────
        _deduct(FACE_SCENE_COST, uid)
        _log_face_scene(FACE_SCENE_COST)
        return {"ok": True, "image_url": img_url, "credits_used": FACE_SCENE_COST}


def _log_face_scene(credits: int):
    log = store.setdefault("api_usage", [])
    log.append({"action": "face_scene", "credits": credits, "ts": datetime.now().isoformat()})
    store["api_usage"] = log[-200:]
    save_store()


@router.get("/api/studio/face-scene/presets")
def face_scene_presets(current_user: dict = Depends(get_current_user)):
    return {"presets": FACE_SCENE_PRESETS, "cost": FACE_SCENE_COST}


# ── Hyper Realistic + Premium Animation (Pro+ premium video providers) ───────
# Both follow a similar request shape: POST {prompt, duration, aspect_ratio}
# with Bearer auth and return a JSON job descriptor. Provider URLs are
# env-configurable so they can be retargeted without a code change.

# Per-duration pricing for premium video providers (Hyper Realistic + Premium
# Animation). 30s long-form clips are rendered by Runway gen4.5. Durations
# outside the table are rejected by _validate_video_payload below.
_VIDEO_CREDIT_PRICES = {5: 50, 10: 85, 12: 95, 15: 100, 30: 120}
_ASPECT_OK           = {"16:9", "9:16", "1:1", "4:5"}
_ALLOWED_DURATIONS   = sorted(_VIDEO_CREDIT_PRICES.keys())


def _video_credit_cost(duration: int) -> int:
    return _VIDEO_CREDIT_PRICES[duration]


async def _call_video_provider(url: str, api_key: str, payload: dict, *, label: str) -> dict:
    """POST `payload` to a Bearer-auth video provider and return its JSON.
    Raises an HTTPException with the provider name on non-2xx."""
    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"{label}: {str(exc)[:160]}")
    if r.status_code >= 400:
        raise HTTPException(status_code=r.status_code, detail=f"{label}: {r.text[:240]}")
    try:
        return r.json()
    except Exception:
        return {"raw": r.text[:500]}


def _validate_video_payload(body: dict) -> tuple[str, int, str, str]:
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")
    if len(prompt) > 1500:
        raise HTTPException(status_code=400, detail="prompt too long (max 1500 chars)")
    try:
        duration = int(body.get("duration") or 5)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="duration must be an integer")
    if duration not in _VIDEO_CREDIT_PRICES:
        raise HTTPException(
            status_code=400,
            detail=f"duration must be one of {_ALLOWED_DURATIONS} seconds",
        )
    aspect = (body.get("aspect_ratio") or "16:9").strip()
    if aspect not in _ASPECT_OK:
        raise HTTPException(status_code=400, detail=f"aspect_ratio must be one of {sorted(_ASPECT_OK)}")
    # Optional reference image (from saved character library). When present
    # it's forwarded to the provider so generations can match an established
    # character look across a mini-series.
    image_url = (body.get("image_url") or "").strip()
    if image_url and len(image_url) > 2000:
        raise HTTPException(status_code=400, detail="image_url too long")
    return prompt, duration, aspect, image_url


def _charge_or_402(uid: str, amount: int) -> int:
    """Deduct `amount` credits from the user's billing or raise 402."""
    ustore = _load_user_store(uid)
    billing = ustore.setdefault("billing", {})
    have = int(billing.get("credits") or 0)
    if have < amount:
        short = amount - have
        raise HTTPException(
            status_code=402,
            detail=f"You need {short} more credits — tap here to top up",
        )
    billing["credits"] = have - amount
    ustore["billing"] = billing
    _save_user_store(uid, ustore)
    return billing["credits"]


def _refund(uid: str, amount: int) -> int:
    """Return `amount` credits to a user (used when a paid generation fails)."""
    ustore = _load_user_store(uid)
    billing = ustore.setdefault("billing", {})
    billing["credits"] = int(billing.get("credits") or 0) + amount
    ustore["billing"] = billing
    _save_user_store(uid, ustore)
    return billing["credits"]


def _extract_video_url(result: dict) -> Optional[str]:
    """Best-effort extraction of a playable URL from a provider response.
    Provider schemas vary, so probe common shapes and return the first match."""
    if not isinstance(result, dict):
        return None
    for key in ("video_url", "url", "output_url", "result_url"):
        v = result.get(key)
        if isinstance(v, str) and v.startswith(("http://", "https://")):
            return v
    for parent_key in ("output", "video", "result", "data"):
        parent = result.get(parent_key)
        if isinstance(parent, dict):
            inner = _extract_video_url(parent)
            if inner:
                return inner
        if isinstance(parent, list) and parent:
            first = parent[0]
            if isinstance(first, str) and first.startswith(("http://", "https://")):
                return first
            if isinstance(first, dict):
                inner = _extract_video_url(first)
                if inner:
                    return inner
    return None


# ── Video generation queue ───────────────────────────────────────────────────
# Premium video renders (Hyper Realistic / Premium Animation / 30s gen4.5) are
# slow and the providers throttle aggressively. Rather than blocking the request
# (and erroring out when throttled), every render is added to a queue:
#   • max MAX_CONCURRENT_VIDEOS render at once;
#   • the rest wait and report their live position;
#   • a throttled/temporarily-unavailable render is automatically re-queued
#     (with backoff) instead of failing;
#   • when a render finishes the user gets an in-app notification and the video
#     appears in My Videos.
MAX_CONCURRENT_VIDEOS = 3
_VQ_MAX_RETRIES       = 6
_VQ_RETRY_BACKOFF_S   = 20      # base seconds; grows per retry

_vq_pending: list = []          # FIFO of job dicts awaiting a free slot
_vq_active:  dict = {}          # job_id -> job dict currently rendering
_vq_jobs:    dict = {}          # job_id -> job dict (all known jobs this session)


def _is_throttle_error(exc: Exception) -> bool:
    """True when an exception looks like a provider being busy / rate-limited /
    temporarily unavailable — i.e. something a retry could fix."""
    status = getattr(exc, "status_code", None)
    if status in (408, 425, 429, 500, 502, 503, 504):
        return True
    msg = str(getattr(exc, "detail", "") or exc).lower()
    return any(s in msg for s in (
        "rate limit", "ratelimit", "too many", "busy", "throttle",
        "unavailable", "timeout", "timed out", "temporarily",
    ))


def _vq_kind_for(provider: str, duration: int) -> str:
    if duration >= 30:
        return "hyper_realistic"      # 30s long-form via Runway gen4.5
    if provider == "luma_ray":
        return "luma_ray"
    return "premium_animation" if provider == "pika" else "hyper_realistic"


def _vq_live_position(job_id: str) -> int:
    """1-based position in the waiting line (0 if it's already rendering/done)."""
    for i, j in enumerate(_vq_pending):
        if j["id"] == job_id:
            return i + 1
    return 0


def _vq_public(job: dict) -> dict:
    return {
        "id":         job["id"],
        "status":     job["status"],          # queued | processing | done | failed
        "provider":   job["provider"],
        "duration":   job["duration"],
        "prompt":     (job.get("prompt") or "")[:80],
        "video_url":  job.get("video_url"),
        "error":      job.get("error"),
        "position":   _vq_live_position(job["id"]),
        "queue_total": len(_vq_pending) + len(_vq_active),
        "active":     len(_vq_active),
        "max":        MAX_CONCURRENT_VIDEOS,
        "created_at": job.get("created_at"),
    }


async def _vq_run_provider(job: dict):
    """Dispatch a job to its provider. Returns (video_url, raw_result).
    Raises on failure (HTTPException carries the provider status code)."""
    provider = job["provider"]
    prompt, duration, aspect, image_url = job["prompt"], job["duration"], job["aspect"], job["image_url"]

    # 30s long-form clips are rendered by Runway gen4.5 regardless of the
    # premium provider the user picked (Kling/Pika top out at 15s).
    if duration >= 30:
        url = await _runway_generate(prompt, image_url, duration, aspect)
        return url, {"provider": "runway_gen45", "video_url": url}

    if provider == "luma_ray":
        url = await _luma_ray_generate(prompt, duration, aspect, image_url)
        return url, {"provider": "luma_ray", "model": LUMA_RAY_MODEL, "video_url": url}

    if provider == "pika":
        payload = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect, "user_id": job["uid"]}
        if image_url:
            payload["image_url"] = image_url
        result = await _call_video_provider(PIKA_API_URL, PIKA_API_KEY, payload, label="Premium Animation")
        return _extract_video_url(result), result

    if provider == "higgsfield":
        # "Hyper Realistic": Higgsfield Seedance primary (up to 12s @1080p,
        # user-selectable length). On ANY failure, transparently fall back to
        # Kling v2.5 — the user only ever sees "Hyper Realistic", never a
        # provider name. If Kling ALSO fails, the exception propagates and the
        # queue refunds the credits.
        uid = job.get("uid", "")
        try:
            url = await _higgsfield_generate(prompt, aspect, image_url, user_id=uid, duration=duration)
            return url, {"provider": "higgsfield", "model": HIGGSFIELD_SEEDANCE_MODEL,
                         "delivered_seconds": _seedance_clip_seconds(duration), "video_url": url}
        except Exception as exc:
            logging.warning("Higgsfield (Seedance) failed, falling back to Kling v2.5: %s",
                            str(getattr(exc, "detail", exc))[:200])
            # Kling tops out at 10s, so a 12s request falls back to a 10s clip.
            url = await _replicate_kling_generate(prompt, duration, aspect, image_url, user_id=uid)
            return url, {"provider": "higgsfield", "engine": "kling_fallback",
                         "delivered_seconds": _kling_clip_seconds(duration), "video_url": url}

    # default: Kling (Hyper Realistic via Replicate)
    url = await _replicate_kling_generate(prompt, duration, aspect, image_url, user_id=job.get("uid", ""))
    return url, {"provider": "kling", "model": KLING_MODEL,
                 "delivered_seconds": _kling_clip_seconds(duration), "video_url": url}


def _vq_pump() -> None:
    """Start as many queued jobs as free slots allow (max MAX_CONCURRENT_VIDEOS)."""
    while _vq_pending and len(_vq_active) < MAX_CONCURRENT_VIDEOS:
        job = _vq_pending.pop(0)
        job["status"] = "processing"
        _vq_active[job["id"]] = job
        asyncio.create_task(_vq_process(job))


async def _vq_requeue_after(job: dict, delay: float) -> None:
    """Put a throttled job back in line after a backoff delay, then pump."""
    await asyncio.sleep(delay)
    job["status"] = "queued"
    _vq_pending.append(job)
    _vq_pump()


async def _vq_process_polish(job: dict) -> None:
    """Run a Phase-1 polish pipeline job off the video queue. Local FFmpeg
    failures are permanent (not throttle) → refund, no retry."""
    uid = job["uid"]
    try:
        result = await asyncio.to_thread(run_polish_pipeline, uid, job["src_url"],
                                         job["steps"], job.get("src_path", ""))
        job["status"]    = "done"
        job["video_url"] = result["url"]
        job["duration"]  = result.get("duration") or 0
        _update_generated_video(uid, job["id"], {
            "url": result["url"], "status": "done", "duration": result.get("duration") or 0,
        })
        try:
            from routes.notifications import push_notification
            push_notification(uid, "video_ready", "🎬 Your polished video is ready",
                              "Your upload has been polished — open My Videos to watch it.")
        except Exception:
            pass
    except Exception as exc:
        job["status"] = "failed"
        job["error"]  = "Something went wrong polishing your video — please try again"
        _refund(uid, job["cost"])
        _update_generated_video(uid, job["id"], {"status": "failed", "error": job["error"]})
        logging.warning("polish job %s failed: %s", job["id"],
                        str(getattr(exc, "detail", "") or exc)[:200])
        try:
            from routes.notifications import push_notification
            push_notification(uid, "video_ready", "⚠️ Polish failed",
                              f"{job['error']}. Your {job['cost']} credits have been refunded.")
        except Exception:
            pass
    finally:
        _vq_active.pop(job["id"], None)
        _vq_pump()


async def _vq_process(job: dict) -> None:
    """Render one job. Frees its slot when done and pumps the next one."""
    # Phase-1 polish jobs run a local FFmpeg pipeline, not a remote provider.
    if job.get("job_type") == "polish":
        await _vq_process_polish(job)
        return
    uid = job["uid"]
    try:
        video_url, raw = await _vq_run_provider(job)
        # Success — record the URL and notify.
        job["status"]    = "done"
        job["video_url"] = video_url
        _update_generated_video(uid, job["id"], {
            "url": video_url, "status": "done",
            "raw_result": raw if isinstance(raw, dict) else None,
        })
        try:
            from routes.content_library import add_to_library
            await add_to_library(
                uid, kind="videos", provider=job["provider"],
                source_url=video_url, prompt=job["prompt"],
                duration=job["duration"], aspect_ratio=job["aspect"],
                credits_used=job["cost"],
            )
        except Exception:
            pass
        try:
            from routes.notifications import push_notification
            push_notification(
                uid, "video_ready", "🎬 Your video is ready",
                f"Your {job['duration']}s video has finished — open My Videos to watch it.",
            )
        except Exception:
            pass
    except Exception as exc:
        if _is_throttle_error(exc) and job["retries"] < _VQ_MAX_RETRIES:
            # Throttled / busy — re-queue automatically, no error shown.
            job["retries"] += 1
            delay = _VQ_RETRY_BACKOFF_S * job["retries"]
            _vq_active.pop(job["id"], None)
            asyncio.create_task(_vq_requeue_after(job, delay))
            _vq_pump()
            return
        # Permanent failure — refund credits and surface a friendly message.
        job["status"] = "failed"
        job["error"]  = "Something went wrong — please try again in a moment"
        _refund(uid, job["cost"])
        _update_generated_video(uid, job["id"], {"status": "failed", "error": job["error"]})
        try:
            from routes.notifications import push_notification
            push_notification(
                uid, "video_ready", "⚠️ Video render failed",
                f"{job['error']}. Your {job['cost']} credits have been refunded.",
            )
        except Exception:
            pass
    finally:
        _vq_active.pop(job["id"], None)
        _vq_pump()


def _enqueue_polish(uid: str, src_url: str, steps: list, cost: int, aspect: str,
                    src_path: str = "") -> dict:
    """Queue a Phase-1 polish job (local FFmpeg pipeline) with a My-Videos
    placeholder. Reuses the video queue's concurrency + refund-on-fail. The input
    URL is kept as `src_url` and the already-resolved local file as `src_path`
    (so the worker doesn't re-download); `video_url` stays None until ready."""
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id, "uid": uid, "job_type": "polish", "provider": "polish",
        "src_url": src_url, "src_path": src_path, "steps": steps, "duration": 0,
        "aspect": aspect, "image_url": "", "cost": cost, "status": "queued",
        "retries": 0, "video_url": None, "error": None,
        "prompt": f"Polish upload → {aspect}",
        "created_at": datetime.now().isoformat(),
    }
    _vq_jobs[job_id] = job
    _vq_pending.append(job)
    _save_generated_video(uid, {
        "id": job_id, "url": None, "provider": "polish", "kind": "polish",
        "duration": 0, "aspect_ratio": aspect, "prompt": job["prompt"][:240],
        "credits_used": cost, "created_at": job["created_at"], "status": "queued",
    })
    _vq_pump()
    return _vq_public(job)


def _enqueue_video(uid: str, provider: str, prompt: str, duration: int,
                   aspect: str, image_url: str, cost: int) -> dict:
    """Create a queued render job, mirror it into My Videos as a placeholder,
    and kick the pump. Returns the public job view (with live position)."""
    job_id = uuid.uuid4().hex[:10]
    job = {
        "id": job_id, "uid": uid, "provider": provider, "prompt": prompt,
        "duration": duration, "aspect": aspect, "image_url": image_url,
        "cost": cost, "status": "queued", "retries": 0,
        "video_url": None, "error": None,
        "created_at": datetime.now().isoformat(),
    }
    _vq_jobs[job_id] = job
    _vq_pending.append(job)
    # Placeholder in My Videos so the user sees it immediately as "processing".
    _save_generated_video(uid, {
        "id": job_id, "url": None, "provider": provider,
        "kind": _vq_kind_for(provider, duration), "duration": duration,
        "aspect_ratio": aspect, "prompt": prompt[:240], "credits_used": cost,
        "created_at": job["created_at"], "status": "queued",
    })
    _vq_pump()
    return _vq_public(job)


def queue_stats() -> dict:
    """Snapshot of the in-memory video render queue (for health monitoring)."""
    by_status: dict = {}
    for j in _vq_jobs.values():
        s = j.get("status", "unknown")
        by_status[s] = by_status.get(s, 0) + 1
    return {
        "pending":          len(_vq_pending),
        "processing":       len(_vq_active),
        "max_concurrent":   MAX_CONCURRENT_VIDEOS,
        "total_tracked":    len(_vq_jobs),
        "by_status":        by_status,
    }


def cleanup_temporary_data(failed_max_age_h: int = 24,
                           url_cache_max_age_days: int = 7) -> dict:
    """Remove ONLY transient, in-memory queue records:

      • failed render jobs older than `failed_max_age_h` hours, and
      • completed ("done") job records older than `url_cache_max_age_days` days —
        these only hold the provider's temporary CDN URL (a cache). The actual
        video stays in the user's generated_videos list and on disk.

    This NEVER touches user_content/ files, content-library items, persisted
    generated_videos records, or anything the user paid credits for. Queued and
    processing jobs are always left alone.
    """
    now = datetime.now()
    removed_failed = 0
    removed_url_cache = 0
    for jid, job in list(_vq_jobs.items()):
        status = job.get("status")
        if status in ("queued", "processing"):
            continue
        try:
            age = now - datetime.fromisoformat(job.get("created_at"))
        except Exception:
            continue
        if status == "failed" and age >= timedelta(hours=failed_max_age_h):
            _vq_jobs.pop(jid, None)
            removed_failed += 1
        elif status == "done" and age >= timedelta(days=url_cache_max_age_days):
            _vq_jobs.pop(jid, None)
            removed_url_cache += 1
    return {
        "failed_jobs_removed":       removed_failed,
        "url_cache_entries_removed": removed_url_cache,
    }


@router.get("/api/studio/queue")
def studio_queue(current_user: dict = Depends(get_current_user)):
    """List the caller's render jobs with live queue positions."""
    uid = current_user["id"]
    jobs = [_vq_public(j) for j in _vq_jobs.values() if j["uid"] == uid]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return {
        "jobs":   jobs,
        "active": len(_vq_active),
        "queued": len(_vq_pending),
        "max":    MAX_CONCURRENT_VIDEOS,
    }


@router.get("/api/studio/queue/{job_id}")
def studio_queue_item(job_id: str, current_user: dict = Depends(get_current_user)):
    job = _vq_jobs.get(job_id)
    if not job or job["uid"] != current_user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return _vq_public(job)


def _provider_configured(provider: str, duration: int) -> bool:
    """Whether the provider needed for this render has an API key configured.
    30s renders use Runway gen4.5, so they need RUNWAY_API_KEY."""
    if duration >= 30:
        return bool(RUNWAY_API_KEY)
    if provider == "luma_ray":
        return bool(LUMA_API_KEY)
    if provider == "pika":
        return bool(PIKA_API_KEY)
    if provider == "higgsfield":
        # Hyper Realistic: Higgsfield primary OR Kling fallback — configured if
        # either is available, so the user always gets a video.
        return bool((HIGGSFIELD_API_KEY_ID and HIGGSFIELD_API_KEY_SECRET) or REPLICATE_API_KEY)
    return bool(REPLICATE_API_KEY)


async def _queue_premium_render(body: dict, uid: str, provider: str) -> dict:
    """Shared entry point for the premium video endpoints. Validates, charges
    credits, and adds the render to the queue. Never errors on provider
    throttling — that's handled by the queue with automatic re-queueing."""
    if not _is_pro_plus(uid):
        raise HTTPException(status_code=403, detail="Premium video generation requires a paid plan")
    # Per-user video-generation rate limit: 10 renders / hour.
    from services.security import check_video_rate_limit, video_rate_limit_message
    if not check_video_rate_limit(uid):
        raise HTTPException(status_code=429, detail=video_rate_limit_message())
    prompt, duration, aspect, image_url = _validate_video_payload(body)
    # Charge for and render the length the provider ACTUALLY delivers as a single
    # clip — never the (possibly longer) requested length. Longer videos are
    # built in the multi-scene studio, not here.
    delivered = _delivered_clip_seconds(provider, duration)
    if not _provider_configured(provider, delivered):
        raise HTTPException(
            status_code=503,
            detail="Our video servers are temporarily unavailable — please try again shortly",
        )
    cost = _video_credit_cost(delivered)
    credits_left = _charge_or_402(uid, cost)
    job = _enqueue_video(uid, provider, prompt, delivered, aspect, image_url, cost)
    if job["status"] == "processing":
        message = "Your video is generating now — it'll appear in My Videos when it's ready"
    else:
        message = f"Your video is generating — position {job['position']} in queue"
    return {
        "queued":       True,
        "job_id":       job["id"],
        "status":       job["status"],
        "position":     job["position"],
        "queue_total":  job["queue_total"],
        "message":      message,
        "provider":     provider,
        "credits_left": credits_left,
        "credits_used": cost,
        "duration":     delivered,
        "request":      {"prompt": prompt, "duration": delivered, "aspect_ratio": aspect, "image_url": image_url},
    }


@router.post("/api/studio/higgsfield")
async def higgsfield_generate(body: dict, current_user: dict = Depends(get_current_user)):
    return await _queue_premium_render(body, current_user["id"], "higgsfield")


@router.post("/api/studio/pika")
async def pika_generate(body: dict, current_user: dict = Depends(get_current_user)):
    return await _queue_premium_render(body, current_user["id"], "pika")


@router.post("/api/studio/kling")
async def kling_generate(body: dict, current_user: dict = Depends(get_current_user)):
    """Hyper Realistic video via Kling (Replicate) — queued.
    Pricing: 50 / 85 / 100 / 120 credits for 5s / 10s / 15s / 30s.
    30s renders are produced by Runway gen4.5."""
    return await _queue_premium_render(body, current_user["id"], "kling")


@router.post("/api/studio/luma")
async def luma_ray_generate_endpoint(body: dict, current_user: dict = Depends(get_current_user)):
    """Luma Ray 3.2 premium video (text-to-video, or image-to-video when an
    image_url/character reference is supplied) — queued.
    Pricing: 50 / 85 / 100 / 120 credits for 5s / 10s / 15s / 30s (same premium
    tier as the other providers). 30s renders are produced by Runway gen4.5."""
    return await _queue_premium_render(body, current_user["id"], "luma_ray")
