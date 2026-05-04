"""
UgoingViral Video Studio — Multi-scene, multi-provider AI video generation + smart clip cutter.
Providers: Runway Gen-3, Luma Dream Machine, Replicate
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
LUMA_API_KEY      = os.getenv("LUMA_API_KEY", "")
REPLICATE_API_KEY = os.getenv("REPLICATE_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
ELEVENLABS_KEY    = os.getenv("ELEVENLABS_API_KEY", "")

UPLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "uploads", "studio")
os.makedirs(UPLOADS_DIR, exist_ok=True)

STUDIO_COSTS = {
    "video_5s":         VIDEO_GENERATION["image_to_video_10s"] // 2,  # 125
    "video_10s":        VIDEO_GENERATION["image_to_video_10s"],        # 250
    "video_15s":        VIDEO_GENERATION["image_to_video_10s"] + VIDEO_GENERATION["extended_per_10s"],   # 300
    "video_30s":        VIDEO_GENERATION["image_to_video_10s"] + VIDEO_GENERATION["extended_per_10s"] * 2,  # 350
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
        "name": "Luma Dream Machine",
        "tagline": "Hyperrealistisk",
        "quality": 5, "speed": 2, "cost_multiplier": 1.2,
        "styles": ["photorealistic", "cinematic", "nature", "sci-fi", "fantasy"],
        "max_duration": 5,
        "available": bool(LUMA_API_KEY),
    },
    "replicate": {
        "name": "Replicate (Kling AI)",
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
    duration: int = 5           # 5, 10, 15 seconds
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
    cost = 0
    for i, scene in enumerate(req.scenes):
        dur_key = f"video_{scene.duration}s" if f"video_{scene.duration}s" in STUDIO_COSTS else "video_5s"
        base = STUDIO_COSTS[dur_key]
        prov = PROVIDERS.get(req.provider, PROVIDERS["runway"])
        base = int(base * prov["cost_multiplier"])
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

    async with httpx.AsyncClient(timeout=60) as client:
        # Create generation
        payload = {
            "promptText": prompt,
            "model": "gen3a_turbo",
            "duration": min(duration, 10),
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

        # Poll for completion (max 3 minutes)
        for _ in range(36):
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
            fn = os.path.join(UPLOADS_DIR, f"voice_{uuid.uuid4().hex[:8]}.mp3")
            with open(fn, "wb") as f:
                f.write(r.content)
            return f"/uploads/studio/{os.path.basename(fn)}"
    except Exception:
        pass
    return None


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
    _save_user_store(uid, ustore)

    return {
        "session_id": session_id,
        "scenes": result_videos,
        "assembled_url": assembled_url,
        "voice_url": voice_url,
        "credits_used": cost,
        "credits_left": credits_left,
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
