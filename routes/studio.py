"""
UgoingViral Video Studio — Multi-scene, multi-provider AI video generation + smart clip cutter.
Providers: Runway Gen-3, Luma Dream Machine, Replicate, Kling AI (Hyper Realistic), Pika (Premium Animation)
"""
import os, uuid, subprocess, json, asyncio, httpx, re, shutil
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, UploadFile, File
from pydantic import BaseModel
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store
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
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY", "")
# Hyper Realistic + Premium Animation (Pro+ video providers). Base URLs are env-configurable
# because both providers gate their public APIs and the canonical endpoint may
# move; defaults below are the documented v1 paths as of May 2026.
HIGGSFIELD_API_KEY = os.getenv("HIGGSFIELD_API_KEY", "")
HIGGSFIELD_API_URL = os.getenv("HIGGSFIELD_API_URL", "https://api.higgsfield.ai/v1/generations")
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
_FREE_VIDEO_DURATION = 10  # seconds — each free trial video


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
    }


def _consume_free_video(user_id: str) -> int:
    """Increment and persist the user's free-video counter; returns new count."""
    ustore = _load_user_store(user_id)
    used = int(ustore.get("free_videos_used", 0) or 0) + 1
    ustore["free_videos_used"] = used
    _save_user_store(user_id, ustore)
    return used


ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_KEY    = os.getenv("ELEVENLABS_API_KEY", "")

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
    ar_map = {"16:9": "1280:720", "9:16": "720:1280", "1:1": "1024:1024", "4:5": "864:1080"}
    wh = ar_map.get(aspect_ratio, "1280:720")

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


# ── Replicate (Kling / CogVideoX) ─────────────────────────────────────────────

async def _replicate_generate(prompt: str, duration: int = 5, aspect_ratio: str = "16:9") -> Optional[str]:
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
                if isinstance(output, list):
                    return output[0]
                return output
            if status == "failed":
                raise HTTPException(status_code=500, detail=f"Replicate fejlede: {data.get('error','')}")
    return None


async def _replicate_kling_generate(prompt: str, duration: int = 5,
                                    aspect_ratio: str = "16:9",
                                    image_url: str = "") -> Optional[str]:
    """Hyper Realistic video via Kling 1.6 Pro on Replicate (klingai/kling-1.6-pro).
    Submits an async prediction and polls until it completes, returning the
    playable video URL."""
    if not REPLICATE_API_KEY:
        raise HTTPException(status_code=503, detail="Replicate API not configured (REPLICATE_API_KEY missing)")
    # Kling accepts a limited set of aspect ratios — map anything else (e.g. 4:5)
    # to the closest supported value so the request never bounces.
    kling_aspect = aspect_ratio if aspect_ratio in ("16:9", "9:16", "1:1") else "9:16"
    pred_input = {
        "prompt":       prompt,
        "duration":     str(duration),       # Kling expects duration as a string
        "aspect_ratio": kling_aspect,
        "cfg_scale":    0.5,
    }
    if image_url:
        # Image-to-video reference for character consistency across a mini-series.
        pred_input["start_image"] = image_url
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.replicate.com/v1/models/klingai/kling-1.6-pro/predictions",
            headers={"Authorization": f"Token {REPLICATE_API_KEY}",
                     "Content-Type": "application/json",
                     "Prefer": "wait"},
            json={"input": pred_input},
        )
        if r.status_code not in (200, 201):
            raise HTTPException(status_code=502, detail=f"Kling AI error {r.status_code}: {r.text[:200]}")
        data = r.json()
        # With "Prefer: wait" Replicate may already return a finished prediction.
        if data.get("status") == "succeeded":
            out = data.get("output")
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
                return out[0] if isinstance(out, list) and out else out
            if st in ("failed", "canceled"):
                raise HTTPException(status_code=502, detail=f"Kling AI generation {st}: {str(pdata.get('error',''))[:160]}")
    raise HTTPException(status_code=504, detail="Kling AI timed out — please try again")


async def _generate_video(prompt: str, provider: str, duration: int,
                          aspect_ratio: str, image_url: str = "") -> str:
    if provider == "runway":
        return await _runway_generate(prompt, image_url, duration, aspect_ratio)
    elif provider == "luma":
        return await _luma_generate(prompt, duration, aspect_ratio)
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
    """Mix voice-over with video (replace or mix audio)."""
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", video_path, "-i", audio_path,
            "-filter_complex", "[1:a]volume=1[a1];[0:a]volume=0.15[a0];[a0][a1]amix=inputs=2:duration=shortest",
            "-c:v", "copy", output_path
        ], capture_output=True, timeout=120)
        return os.path.exists(output_path)
    except Exception:
        return False


# ── API Endpoints ──────────────────────────────────────────────────────────────

@router.get("/api/studio/providers")
def get_providers(current_user: dict = Depends(get_current_user)):
    return {
        "providers": PROVIDERS,
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

    cost = _calc_cost(req)
    uid = current_user["id"]

    # ── Free trial videos ──────────────────────────────────────────────────
    # Free-plan users get 3 free AI videos (generated via Runway) before any
    # credit charge. Once the allowance is used up, video generation is gated
    # behind a paid plan.
    fv = _free_videos_status(uid)
    used_free_video = False
    if fv["eligible"]:
        if fv["remaining"] <= 0:
            raise HTTPException(
                status_code=403,
                detail="You've used all 3 free videos. Upgrade to a paid plan to keep generating videos.",
            )
        # Free trial videos render via Runway when it's configured.
        if PROVIDERS.get("runway", {}).get("available"):
            provider = "runway"
        cost = 0
        used_free_video = True
        _consume_free_video(uid)
        credits_left = _load_user_store(uid).get("billing", {}).get("credits", 0)
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
_VIDEO_CREDIT_PRICES = {5: 50, 10: 85, 15: 100, 30: 120}
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

    if provider == "pika":
        payload = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect, "user_id": job["uid"]}
        if image_url:
            payload["image_url"] = image_url
        result = await _call_video_provider(PIKA_API_URL, PIKA_API_KEY, payload, label="Premium Animation")
        return _extract_video_url(result), result

    if provider == "higgsfield":
        payload = {"prompt": prompt, "duration": duration, "aspect_ratio": aspect,
                   "metadata": {"user_id": job["uid"], "source": "ugoingviral"}}
        if image_url:
            payload["image_url"] = image_url
        result = await _call_video_provider(HIGGSFIELD_API_URL, HIGGSFIELD_API_KEY, payload, label="Hyper Realistic")
        return _extract_video_url(result), result

    # default: Kling (Hyper Realistic via Replicate)
    url = await _replicate_kling_generate(prompt, duration, aspect, image_url)
    return url, {"provider": "kling", "video_url": url}


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


async def _vq_process(job: dict) -> None:
    """Render one job. Frees its slot when done and pumps the next one."""
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
    if provider == "pika":
        return bool(PIKA_API_KEY)
    if provider == "higgsfield":
        return bool(HIGGSFIELD_API_KEY)
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
    if not _provider_configured(provider, duration):
        raise HTTPException(
            status_code=503,
            detail="Our video servers are temporarily unavailable — please try again shortly",
        )
    cost = _video_credit_cost(duration)
    credits_left = _charge_or_402(uid, cost)
    job = _enqueue_video(uid, provider, prompt, duration, aspect, image_url, cost)
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
        "request":      {"prompt": prompt, "duration": duration, "aspect_ratio": aspect, "image_url": image_url},
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
