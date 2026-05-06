from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil, subprocess, uuid, tempfile
from datetime import datetime
from pydantic import BaseModel
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()

PIPELINE_MODEL = "claude-sonnet-4-6"
PIPELINE_PLATFORMS = ("instagram", "tiktok", "youtube")

# Credit costs per action
import sys as _sys
import os as _os
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), ".."))
from credit_costs import VIDEO_GENERATION, VIDEO_EDITING, VOICE_OVER

CREDIT_COSTS = {
    "generate_pipeline":  5,
    "generate_video":     VIDEO_GENERATION["image_to_video_10s"],   # 250
    "generate_video_15":  VIDEO_GENERATION["image_to_video_10s"] + VIDEO_GENERATION["extended_per_10s"],  # 300
    "generate_video_30":  VIDEO_GENERATION["image_to_video_10s"] + VIDEO_GENERATION["extended_per_10s"] * 2,  # 350
    "generate_voice":     VOICE_OVER["per_30s"],                    # 10
    "assemble_video":     VIDEO_EDITING["upload_and_edit"],         # 50
    "add_captions":       VIDEO_EDITING["effects_filter"],          # 25
    "auto_post":          1,
}


def _deduct_credits(action: str) -> int:
    """Deduct credits for action. Returns credits remaining. Raises 402 if insufficient."""
    from routes.billing import PLANS
    amount  = CREDIT_COSTS.get(action, 1)
    billing = store.setdefault("billing", {})
    plan_key  = billing.get("plan", "free")
    max_credits = PLANS.get(plan_key, PLANS["free"])["credits"]
    current   = billing.get("credits", max_credits)
    if current < amount:
        raise HTTPException(
            status_code=402,
            detail="Insufficient credits. Please top up to continue."
        )
    billing["credits"] = current - amount
    store["billing"] = billing
    log = store.setdefault("api_usage", [])
    log.append({"action": action, "credits": amount, "ts": datetime.now().isoformat()})
    store["api_usage"] = log[-200:]
    save_store()
    return billing["credits"]


class PipelineRequest(BaseModel):
    product_title: str
    product_description: str = ""
    target_audience: str = ""
    platforms: List[str] = ["instagram", "tiktok", "youtube"]
    style: str = "energisk"
    image_urls: List[str] = []


# ── AI ────────────────────────────────────────────────────────────────────────
def _build_prompt(req: ContentRequest) -> str:
    lang = {"da": "Skriv KUN på dansk.", "en": "Write ONLY in English.", "both": "Write in both Danish and English."}.get(req.language, "Write ONLY in English.")
    ptips = {"instagram": "emojis, max 2200 tegn, stærk åbning", "tiktok": "ultra kort, hook i 2 sek",
             "facebook": "info, CTA", "twitter": "max 280 tegn, direkte og fængende"}.get(req.platform, "")
    tmap = {
        "caption": "Engagerende caption med emojis og CTA.",
        "hook": "ÉN scroll-stoppende hook-sætning (max 8 ord).",
        "reel_script": "Reel script: [HOOK 0-2s] [PROBLEM 2-5s] [LØSNING 5-20s] [CTA 20-30s]",
        "hashtags": "20 hashtags. Mix store+niche. Kun hashtags.",
        "video_description": "Video titel + 2 sætninger beskrivelse + 10 tags.",
        "tweet": "Fængende tweet max 280 tegn. Direkte og engagerende.",
    }
    prod = f"\nProdukt: {req.product_title}" if req.product_title else ""
    desc = f"\nInfo: {req.product_description[:200]}" if req.product_description else ""

    # Add creator context
    creator_ctx = ""
    if req.creator_id:
        creator = next((c for c in store.get("creators", []) if c["id"] == req.creator_id), None)
        if creator:
            creator_ctx = f"\nCreator: {creator['name']}. Bio: {creator.get('bio', '')}. Skriv i deres stil."

    return f"{lang}\nEkspert social media creator. Platform: {req.platform.upper()} ({ptips}).{prod}{desc}{creator_ctx}\nOpgave: {tmap.get(req.content_type, 'Lav content.')}\nTone: {req.tone}\nSvar KUN med content."

def _ghost_prompt(message: str, sender: str, dm: dict) -> str:
    lang = {"da": "dansk", "en": "engelsk"}.get(dm.get("reply_language", "da"), "dansk")
    ghost = """GHOST MODE: Du ER en rigtig person i butikken — ALDRIG en AI/bot.
Skriv naturligt som et menneske. Afslappet dansk, personligt.
Sig aldrig "som AI", "automatisk svar" eller lignende.
Hvis nogen spørger om du er bot: svar naturligt og undvigende som et menneske ville.
Variér dine svar — aldrig ens.""" if dm.get("ghost_mode", True) else ""
    forbidden = f"\nMå IKKE nævne: {dm['forbidden_topics']}" if dm.get("forbidden_topics") else ""
    allowed = f"\nKun svar på: {dm['allowed_topics']}" if dm.get("allowed_topics") else ""
    custom = f"\nInstruktioner: {dm['custom_instructions']}" if dm.get("custom_instructions") else ""
    return f"""Du svarer for en webshop til {sender or 'kunde'}.
Tone: {dm.get('reply_tone', 'venlig')}. Skriv på {lang}. Max 3 sætninger. Brug emojis naturligt.
{ghost}{custom}{forbidden}{allowed}
Kundens besked: "{message}"
Svar direkte og naturligt."""

async def _call_ai(prompt: str) -> str:
    import os
    s = store.get("settings", {})
    
    # Try Anthropic (user key first, then env)
    anthropic_key = (s.get("anthropic_key","") if s.get("anthropic_key") and "••••" not in s.get("anthropic_key","") else "") or os.getenv("ANTHROPIC_API_KEY","")
    if anthropic_key:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5", "max_tokens": 800, "messages": [{"role": "user", "content": prompt}]},
                    timeout=30)
                r.raise_for_status()
                return r.json()["content"][0]["text"]
        except: pass
    
    # Try OpenAI (user key first, then env)
    openai_key = (s.get("openai_key","") if s.get("openai_key") and "••••" not in s.get("openai_key","") else "") or os.getenv("OPENAI_API_KEY","")
    if openai_key:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}", "content-type": "application/json"},
                    json={"model": "gpt-4o-mini", "max_tokens": 800, "messages": [{"role": "system", "content": "You are a social media content expert. Always respond in English only. Never use any other language."}, {"role": "user", "content": "You must respond in English only. Do not use any other language.\n\n" + prompt}]},
                    timeout=30)
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except: pass
    demos = {
        "caption": "🔥 Transform your body this week!\n\nConsistency is the key to results. Start today! 💪\n\n#fitness #motivation #health #gym #workout",
        "hook": "⚡ Stop — dette SKAL du se!",
        "reel_script": "[HOOK 0-2s] Vidste du dette?! 🤯\n[PROBLEM 2-5s] Du spilder tid\n[LØSNING 5-20s] Vi gjort det nemt 👇\n[CTA 20-30s] Link i bio — gratis fragt!",
        "hashtags": "#trending #viral #fitness #health #gym #workout #motivation #lifestyle #wellness #fitnessmotivation #fyp #foryoupage #explore #instagood #reels",
        "tweet": "⚡ Nyt produkt der er ved at sælge ud — se det nu! Link i bio 🔥 #trending #nyt",
    }
    for k in demos:
        if k in prompt.lower(): return demos[k]
    return demos["caption"]

# ── Content ───────────────────────────────────────────────────────────────────
@router.post("/api/content/generate")
async def generate_content(req: ContentRequest):
    from routes.viral_score import _rule_score

    result = await _call_ai(_build_prompt(req))

    # Score content — auto-regenerate once if score is too low
    def _quick_score(text: str) -> int:
        hashtags = [w for w in text.split() if w.startswith("#")]
        try:
            return _rule_score(text, hashtags, req.platform or "instagram", "")["score"]
        except Exception:
            return 50

    score = _quick_score(result)
    if score < 40:
        regen = await _call_ai(_build_prompt(req))
        regen_score = _quick_score(regen)
        if regen_score > score:
            result, score = regen, regen_score

    item = {"id": datetime.now().isoformat(), "type": req.content_type, "platform": req.platform,
            "product_id": req.product_id, "product": req.product_title,
            "creator_id": req.creator_id, "content": result,
            "content_score": score, "created": datetime.now().isoformat()}
    store.get("content_history", {}).insert(0, item)
    if len(store.get("content_history", {})) > 200: store["content_history"] = store.get("content_history", {})[:200]
    if req.product_id:
        pid_str = str(req.product_id)
        if pid_str not in store.get("product_content", {}): store.get("product_content", {})[pid_str] = []
        store.get("product_content", {})[pid_str].insert(0, item)
    save_store()
    return {"content": result, "id": item["id"], "content_score": score}

@router.post("/api/content/generate_all")
async def generate_all(req: ContentRequest):
    from routes.viral_score import _rule_score

    def _quick_score(text: str, ctype: str) -> int:
        if ctype not in ("caption", "hook", "reel_script", "tweet"):
            return 50
        hashtags = [w for w in text.split() if w.startswith("#")]
        try:
            return _rule_score(text, hashtags, req.platform or "instagram", "")["score"]
        except Exception:
            return 50

    results = {}
    scores  = {}
    types = ["caption", "hook", "hashtags", "reel_script"]
    if req.platform == "twitter": types = ["tweet", "hashtags"]
    for ctype in types:
        req.content_type = ctype
        text = await _call_ai(_build_prompt(req))
        sc   = _quick_score(text, ctype)
        if sc < 40 and ctype in ("caption", "tweet"):
            regen    = await _call_ai(_build_prompt(req))
            regen_sc = _quick_score(regen, ctype)
            if regen_sc > sc:
                text, sc = regen, regen_sc
        results[ctype] = text
        scores[ctype]  = sc
        item = {"id": f"{datetime.now().isoformat()}-{ctype}", "type": ctype, "platform": req.platform,
                "product_id": req.product_id, "product": req.product_title,
                "content": text, "content_score": sc, "created": datetime.now().isoformat()}
        store.get("content_history", {}).insert(0, item)
        if req.product_id:
            pid_str = str(req.product_id)
            if pid_str not in store.get("product_content", {}): store.get("product_content", {})[pid_str] = []
            store.get("product_content", {})[pid_str].insert(0, item)
    save_store()
    return {**results, "scores": scores}

@router.get("/api/content/history")
def get_history(): return {"history": store.get("content_history", {})}

@router.delete("/api/content/history")
def clear_history():
    store["content_history"] = []; save_store(); return {"status": "cleared"}

# ── DM Reply ──────────────────────────────────────────────────────────────────
@router.post("/api/dm/reply")
async def dm_reply(req: DMReplyRequest):
    dm = store.get("dm_settings", {})
    reply = await _call_ai(_ghost_prompt(req.message, req.sender_name, dm))
    return {"reply": reply, "ghost_mode": dm.get("ghost_mode", True)}


# ── Content Pipeline ──────────────────────────────────────────────────────────

def _pipeline_prompt(req: PipelineRequest, platform: str) -> str:
    tips = {
        "instagram": "vertical 9:16 Reels, hook inden for 2 sek, max 2200 tegn caption, emojis",
        "tiktok":    "vertical 9:16, ultra-kort hook 0-2 sek, snappy klip, trending energi",
        "youtube":   "YouTube Shorts 9:16 eller standard, klar titel, keywords i beskrivelse",
    }.get(platform, platform)
    imgs = f"\nBilleder/video: {', '.join(req.image_urls[:3])}" if req.image_urls else ""
    return f"""Du er ekspert social media content creator og videoproducer.

Produkt: {req.product_title}
Beskrivelse: {req.product_description or 'Ikke angivet'}
Målgruppe: {req.target_audience or 'Bredt publikum'}
Platform: {platform.upper()} — {tips}
Stil: {req.style}{imgs}

Generer præcis følgende som ren JSON (ingen markdown):
{{
  "hooks":     ["10 scroll-stoppende hooks, max 8 ord hver"],
  "scripts":   ["10 komplette video scripts med [HOOK 0-2s] [PROBLEM 2-5s] [LØSNING 5-20s] [CTA 20-30s]"],
  "shotlists": ["10 konkrete shotlists med kameravinkel, handling og lydnotes"],
  "captions":  ["10 færdige captions med emojis og CTA"],
  "hashtags":  "30 relevante {platform} hashtags som én streng",
  "call_to_action": "Den stærkeste CTA til dette produkt"
}}

Skriv på dansk. Svar KUN med JSON."""


async def _call_pipeline_api(prompt: str) -> dict:
    api_key = store.get("settings", {}).get("anthropic_key", "") or os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or "••••" in api_key:
        raise HTTPException(status_code=400, detail="Anthropic API nøgle mangler — tilføj den under Indstillinger.")
    async with httpx.AsyncClient() as c:
        r = await c.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": PIPELINE_MODEL, "max_tokens": 8000, "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        r.raise_for_status()
        text = r.json()["content"][0]["text"].strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip())


@router.post("/api/content/generate_pipeline")
async def generate_pipeline(req: PipelineRequest):
    credits_left = _deduct_credits("generate_pipeline")
    platforms = [p.lower() for p in req.platforms if p.lower() in PIPELINE_PLATFORMS] or list(PIPELINE_PLATFORMS)
    store.setdefault("content_pipeline", {})
    results = {}
    for platform in platforms:
        data = await _call_pipeline_api(_pipeline_prompt(req, platform))
        store.get("content_pipeline", {})[platform] = {
            **data,
            "product_title":   req.product_title,
            "target_audience": req.target_audience,
            "style":           req.style,
            "image_urls":      req.image_urls,
            "generated_at":    datetime.now().isoformat(),
        }
        results[platform] = data
    save_store()
    return {"status": "ok", "platforms": platforms, "results": results, "credits_left": credits_left}


@router.get("/api/content/pipeline")
async def get_pipeline():
    return {"pipeline": store.get("content_pipeline", {})}


# ── Runway Video Generation ───────────────────────────────────────────────────

RUNWAY_API    = "https://api.dev.runwayml.com/v1"
RUNWAY_MODEL  = "gen4_turbo"
RUNWAY_VERSION = "2024-11-06"

RATIO_MAP = {
    "instagram": "768:1280",   # 9:16 Reels
    "tiktok":    "768:1280",   # 9:16 TikTok
    "youtube":   "1280:720",   # 16:9 standard — Shorts bruger 768:1280
    "shorts":    "768:1280",
}


class VideoGenerateRequest(BaseModel):
    shotlist: str
    image_url: str = ""
    platform: str = "instagram"
    duration: int = 5          # 5 eller 10 sekunder
    ratio: str = ""            # auto hvis tom


async def _runway_create_and_poll(shotlist: str, image_url: str, duration: int, ratio: str) -> tuple:
    api_key = store.get("settings", {}).get("runway_key", "") or os.getenv("RUNWAY_API_KEY", "")
    if not api_key or "••••" in api_key:
        raise HTTPException(status_code=400, detail="Runway API nøgle mangler — tilføj den under Indstillinger.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Runway-Version": RUNWAY_VERSION,
    }
    body: dict = {
        "model":       RUNWAY_MODEL,
        "prompt_text": shotlist[:512],
        "ratio":       ratio,
        "duration":    duration,
    }
    if image_url:
        body["prompt_image"] = image_url

    async with httpx.AsyncClient() as c:
        # Opret task
        r = await c.post(f"{RUNWAY_API}/image_to_video", headers=headers, json=body, timeout=30)
        if r.status_code != 200:
            detail = r.json().get("error", r.text)[:300] if r.content else r.status_code
            raise HTTPException(status_code=r.status_code, detail=f"Runway: {detail}")
        task_id = r.json()["id"]

        # Poll indtil færdig — max 4 min (48 × 5s)
        for _ in range(48):
            await asyncio.sleep(5)
            poll = await c.get(f"{RUNWAY_API}/tasks/{task_id}", headers=headers, timeout=15)
            poll.raise_for_status()
            data = poll.json()
            status = data.get("status")
            if status == "SUCCEEDED":
                output = data.get("output") or []
                if output:
                    return task_id, output[0]
                raise HTTPException(status_code=500, detail="Runway: ingen video URL i svar")
            if status in ("FAILED", "CANCELED"):
                err = data.get("failure", status)
                raise HTTPException(status_code=500, detail=f"Runway task fejlede: {err}")

    raise HTTPException(status_code=504, detail="Runway video generation timeout (4 min)")


@router.post("/api/content/generate_video")
async def generate_video(req: VideoGenerateRequest):
    credits_left = _deduct_credits("generate_video")
    ratio = req.ratio or RATIO_MAP.get(req.platform, "768:1280")
    duration = max(5, min(10, req.duration))

    task_id, video_url = await _runway_create_and_poll(
        req.shotlist, req.image_url, duration, ratio
    )

    # Gem i brugerens pipeline data under platform → videos
    store.setdefault("content_pipeline", {})
    plat_data = store.get("content_pipeline", {}).setdefault(req.platform, {})
    videos = plat_data.setdefault("videos", [])
    videos.insert(0, {
        "task_id":      task_id,
        "url":          video_url,
        "shotlist":     req.shotlist,
        "duration":     duration,
        "ratio":        ratio,
        "generated_at": datetime.now().isoformat(),
    })
    plat_data["videos"] = videos[:20]   # max 20 gemte videoer
    save_store()

    # Auto bonus: first video ever
    try:
        from routes.billing import _try_auto_bonus
        _try_auto_bonus("first_video")
    except Exception:
        pass

    return {
        "status":      "ok",
        "task_id":     task_id,
        "video_url":   video_url,
        "platform":    req.platform,
        "duration":    duration,
        "ratio":       ratio,
        "credits_left": credits_left,
    }


@router.get("/api/content/videos")
async def get_videos(platform: str = ""):
    pipeline = store.get("content_pipeline", {})
    if platform:
        return {"videos": pipeline.get(platform, {}).get("videos", [])}
    all_videos = []
    for p, data in pipeline.items():
        for v in data.get("videos", []):
            all_videos.append({**v, "platform": p})
    all_videos.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
    return {"videos": all_videos}


# ── ElevenLabs Voiceover ──────────────────────────────────────────────────────

ELEVENLABS_API  = "https://api.elevenlabs.io/v1"
ELEVENLABS_MODEL = "eleven_multilingual_v2"

# Standard stemme-IDs — kan overskrives via request
VOICE_IDS = {
    "da": "pNInz6obpgDQGcFmaJgB",   # Adam — multilingual, fungerer godt med dansk
    "en": "21m00Tcm4TlvDq8ikWAM",   # Rachel — engelsk, klar og naturlig
}


class VoiceGenerateRequest(BaseModel):
    script: str
    language: str = "da"        # "da" eller "en"
    voice_id: str = ""          # valgfri override
    platform: str = "instagram"
    stability: float = 0.5
    similarity_boost: float = 0.75
    style: float = 0.0          # 0.0–1.0, høj = mere ekspressiv


@router.post("/api/content/generate_voice")
async def generate_voice(req: VoiceGenerateRequest):
    credits_left = _deduct_credits("generate_voice")
    api_key = os.getenv("ELEVENLABS_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=400, detail="ELEVENLABS_API_KEY mangler på serveren.")

    lang = req.language.lower()[:2]
    voice_id = req.voice_id.strip() or VOICE_IDS.get(lang, VOICE_IDS["en"])

    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    body = {
        "text": req.script[:5000],
        "model_id": ELEVENLABS_MODEL,
        "voice_settings": {
            "stability":        max(0.0, min(1.0, req.stability)),
            "similarity_boost": max(0.0, min(1.0, req.similarity_boost)),
            "style":            max(0.0, min(1.0, req.style)),
            "use_speaker_boost": True,
        },
    }

    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{ELEVENLABS_API}/text-to-speech/{voice_id}",
            headers=headers,
            json=body,
            timeout=60,
        )
        if r.status_code != 200:
            detail = r.json().get("detail", {}).get("message", r.text[:200]) if r.content else str(r.status_code)
            raise HTTPException(status_code=r.status_code, detail=f"ElevenLabs: {detail}")

        audio_bytes = r.content

    # Gem MP3 i uploads/voice/
    voice_dir = os.path.join(os.getcwd(), "uploads", "voice")
    os.makedirs(voice_dir, exist_ok=True)
    filename  = f"voice_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{lang}.mp3"
    filepath  = os.path.join(voice_dir, filename)
    with open(filepath, "wb") as f:
        f.write(audio_bytes)

    audio_url = f"/uploads/voice/{filename}"

    # Gem i brugerens pipeline data under platform → voices
    store.setdefault("content_pipeline", {})
    plat_data = store.get("content_pipeline", {}).setdefault(req.platform, {})
    voices = plat_data.setdefault("voices", [])
    voices.insert(0, {
        "url":          audio_url,
        "language":     lang,
        "voice_id":     voice_id,
        "script":       req.script[:300],
        "generated_at": datetime.now().isoformat(),
    })
    plat_data["voices"] = voices[:20]
    save_store()

    return {
        "status":      "ok",
        "audio_url":   audio_url,
        "voice_url":   audio_url,
        "language":    lang,
        "voice_id":    voice_id,
        "platform":    req.platform,
        "bytes":       len(audio_bytes),
        "credits_left": credits_left,
    }


@router.get("/api/content/voices")
async def get_voices(platform: str = ""):
    pipeline = store.get("content_pipeline", {})
    if platform:
        return {"voices": pipeline.get(platform, {}).get("voices", [])}
    all_voices = []
    for p, data in pipeline.items():
        for v in data.get("voices", []):
            all_voices.append({**v, "platform": p})
    all_voices.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
    return {"voices": all_voices}


# ── Video Assembly (FFmpeg) ───────────────────────────────────────────────────

class AssembleRequest(BaseModel):
    video_url: str
    voice_url: str
    platform: str = "instagram"


async def _fetch_to_tmp(url: str, suffix: str) -> str:
    """Download lokal eller ekstern URL til temp-fil. Returnerer sti."""
    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    if url.startswith("/uploads/"):
        src = os.path.join(os.getcwd(), url.lstrip("/").replace("/", os.sep))
        shutil.copy2(src, tmp.name)
    else:
        async with httpx.AsyncClient(follow_redirects=True) as c:
            r = await c.get(url, timeout=90)
            r.raise_for_status()
            with open(tmp.name, "wb") as f:
                f.write(r.content)
    return tmp.name


def _run_ffmpeg(video_path: str, audio_path: str, output_path: str, watermark: bool = False) -> str:
    """Kør FFmpeg synkront — returnerer stderr ved fejl, tom streng ved succes."""
    wm_filter = (
        "drawtext=text='Made with UgoingViral'"
        ":fontcolor=white:fontsize=14"
        ":x=w-tw-12:y=h-th-12"
        ":shadowcolor=black:shadowx=1:shadowy=1"
        ":alpha=0.65"
    )
    if watermark:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-filter_complex", f"[0:v]{wm_filter}[vout]",
            "-map", "[vout]",
            "-map", "1:a:0",
            "-c:v", "libx264", "-preset", "fast", "-crf", "22",
            "-c:a", "aac",
            "-shortest",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-i", audio_path,
            "-c:v", "copy",
            "-c:a", "aac",
            "-map", "0:v:0",
            "-map", "1:a:0",
            "-shortest",
            output_path,
        ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return "" if result.returncode == 0 else result.stderr[-600:]


@router.post("/api/content/assemble_video")
async def assemble_video(req: AssembleRequest):
    credits_left = _deduct_credits("assemble_video")
    # Tjek FFmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
    except Exception:
        raise HTTPException(status_code=500, detail="FFmpeg er ikke installeret på serveren. Kør: apt-get install -y ffmpeg")

    # Download begge filer til temp
    tmp_video = tmp_audio = None
    try:
        tmp_video = await _fetch_to_tmp(req.video_url, ".mp4")
        tmp_audio = await _fetch_to_tmp(req.voice_url, ".mp3")

        # Output mappe
        final_dir = os.path.join(os.getcwd(), "uploads", "final")
        os.makedirs(final_dir, exist_ok=True)
        filename    = f"final_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.mp4"
        output_path = os.path.join(final_dir, filename)

        # Kør FFmpeg i thread-executor så vi ikke blokerer event loop
        billing = store.get("billing", {})
        plan = billing.get("plan", "free")
        profile = store.get("profile", {})
        watermark = (plan == "free") or profile.get("watermark_enabled", False)
        loop = asyncio.get_event_loop()
        err  = await loop.run_in_executor(None, _run_ffmpeg, tmp_video, tmp_audio, output_path, watermark)
        if err:
            raise HTTPException(status_code=500, detail=f"FFmpeg fejl: {err}")

    finally:
        for f in [tmp_video, tmp_audio]:
            if f:
                try: os.unlink(f)
                except: pass

    final_url = f"/uploads/final/{filename}"

    # Gem i brugerens pipeline data
    store.setdefault("content_pipeline", {})
    plat_data = store.get("content_pipeline", {}).setdefault(req.platform, {})
    finals    = plat_data.setdefault("finals", [])
    finals.insert(0, {
        "url":          final_url,
        "video_url":    req.video_url,
        "voice_url":    req.voice_url,
        "generated_at": datetime.now().isoformat(),
    })
    plat_data["finals"] = finals[:10]
    save_store()

    return {"status": "ok", "final_url": final_url, "platform": req.platform, "credits_left": credits_left}


@router.get("/api/content/finals")
async def get_finals(platform: str = ""):
    pipeline = store.get("content_pipeline", {})
    if platform:
        return {"finals": pipeline.get(platform, {}).get("finals", [])}
    all_finals = []
    for p, data in pipeline.items():
        for f in data.get("finals", []):
            all_finals.append({**f, "platform": p})
    all_finals.sort(key=lambda x: x.get("generated_at", ""), reverse=True)
    return {"finals": all_finals}


@router.post("/api/content/upload_image")
async def upload_image(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "img.jpg")[1].lower() or ".jpg"
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    if ext not in allowed:
        raise HTTPException(status_code=400, detail="Kun JPG, PNG, WebP, GIF tilladt.")
    filename = f"{uuid.uuid4().hex}{ext}"
    dest = os.path.join("uploads", filename)
    with open(dest, "wb") as out:
        out.write(await file.read())
    return {"url": f"/uploads/{filename}"}


class CaptionRequest(BaseModel):
    video_url: str
    caption: str
    platform: str = ""


def _wrap_caption(text: str, chars_per_line: int = 36) -> str:
    import textwrap
    return "\n".join(textwrap.wrap(text.strip(), chars_per_line))


def _run_ffmpeg_captions(video_path: str, cap_file: str, output_path: str) -> str:
    vf = (
        f"drawtext=textfile='{cap_file}'"
        ":fontsize=54"
        ":fontcolor=white"
        ":borderw=3"
        ":bordercolor=black"
        ":x=(w-text_w)/2"
        ":y=h-text_h-80"
        ":font='DejaVu Sans Bold'"
        ":fix_bounds=1"
        ":line_spacing=10"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    return "" if result.returncode == 0 else result.stderr[-800:]


@router.post("/api/content/add_captions")
async def add_captions(req: CaptionRequest):
    credits_left = _deduct_credits("add_captions")
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True, timeout=5)
    except Exception:
        raise HTTPException(status_code=500, detail="FFmpeg er ikke installeret.")

    cap_tmp = f"/tmp/ugv_cap_{uuid.uuid4().hex}.txt"
    with open(cap_tmp, "w", encoding="utf-8") as f:
        f.write(_wrap_caption(req.caption))

    tmp_video = await _fetch_to_tmp(req.video_url, ".mp4")
    out_filename = f"cap_{uuid.uuid4().hex}.mp4"
    out_path = os.path.join("uploads", "final", out_filename)

    loop = asyncio.get_event_loop()
    err = await loop.run_in_executor(None, _run_ffmpeg_captions, tmp_video, cap_tmp, out_path)

    for f in (cap_tmp, tmp_video):
        try: os.unlink(f)
        except Exception: pass

    if err:
        raise HTTPException(status_code=500, detail=f"FFmpeg fejl: {err}")

    captioned_url = f"/uploads/final/{out_filename}"
    if req.platform:
        store.setdefault("content_pipeline", {})
        plat_data = store.get("content_pipeline", {}).setdefault(req.platform, {})
        finals = plat_data.setdefault("finals", [])
        finals.insert(0, {"url": captioned_url, "generated_at": datetime.now().isoformat(), "type": "captioned"})
        plat_data["finals"] = finals[:10]
        save_store()

    return {"captioned_url": captioned_url}


# ── Content Plan ──────────────────────────────────────────────────────────────
from fastapi import Depends
from routes.auth import get_current_user

CONTENT_PLAN_COST = 10
AUTOPILOT_MIN_CREDITS = 200

class ContentPlanRequest(BaseModel):
    niche: str
    goal: str
    posts_per_day: int = 2
    platforms: list = []
    tone: str = "Professional"
    language: str = "English"

@router.post("/api/content/create-plan")
async def create_content_plan(req: ContentPlanRequest, current_user: dict = Depends(get_current_user)):
    from routes.billing import PLANS
    billing = store.get("billing", {})
    plan_key = billing.get("plan", "free")

    if plan_key == "free":
        raise HTTPException(status_code=403, detail="Content Plan requires a paid plan. Please upgrade.")

    plan_info = PLANS.get(plan_key, PLANS["free"])
    current_credits = billing.get("credits", plan_info["credits"])
    if current_credits < CONTENT_PLAN_COST:
        raise HTTPException(status_code=402, detail=f"Not enough credits. Need {CONTENT_PLAN_COST} to generate a plan.")

    billing["credits"] = current_credits - CONTENT_PLAN_COST
    store["billing"] = billing
    log = store.setdefault("api_usage", [])
    log.append({"action": "content_plan", "credits": CONTENT_PLAN_COST, "ts": datetime.now().isoformat()})
    store["api_usage"] = log[-200:]
    save_store()

    platforms_str = ", ".join(req.platforms) if req.platforms else "Instagram, TikTok"
    from datetime import timedelta
    start_date = datetime.now()

    days_info = []
    for i in range(7):
        d = start_date + timedelta(days=i)
        days_info.append(f"Day {i+1}: {d.strftime('%Y-%m-%d')}")
    days_block = "\n".join(days_info)

    # Gather smart context from user data
    products = store.get("manual_products", [])
    content_history = store.get("content_history", [])[-5:]

    context_lines = []
    if products:
        prod_names = [p.get("name","") for p in products[:8] if p.get("name")]
        if prod_names:
            context_lines.append(f"Products in webshop: {', '.join(prod_names)}")
    if content_history:
        recent_topics = [h.get("topic") or h.get("content","")[:40] for h in content_history if h.get("topic") or h.get("content")]
        if recent_topics:
            context_lines.append(f"Recent content topics: {'; '.join(recent_topics)}")
    context_block = "\n".join(context_lines) if context_lines else "No additional context available."

    # Product field in JSON if products exist
    product_field = ""
    if products:
        product_field = '          "product_name": "exact product name if this post features a specific product, else null",'

    first_platform = req.platforms[0] if req.platforms else "instagram"

    prompt = f"""You are a professional social media strategist. Create a 7-day content plan.

Client info:
- Niche/Industry: {req.niche}
- Primary Goal: {req.goal}
- Posts per day: {req.posts_per_day}
- Platforms: {platforms_str}
- Tone of Voice: {req.tone}
- Language: {req.language}

User context:
{context_block}

Dates:
{days_block}

Return ONLY valid JSON — no markdown fences, no explanation. Use this exact structure:
{{
  "summary": "One sentence strategy overview",
  "total_credits_estimate": <integer, sum of all post credit estimates>,
  "days": [
    {{
      "day": 1,
      "date": "YYYY-MM-DD",
      "posts": [
        {{
          "post_type": "video|image|text|story",
          "platform": "{first_platform}",
          "best_time": "HH:MM",
{product_field}
          "caption_style": "Brief style description",
          "caption_example": "Ready-to-use caption with emojis and CTA in {req.language}",
          "hashtag_strategy": "Brief strategy note",
          "hashtags": ["#tag1","#tag2","#tag3","#tag4","#tag5"],
          "credits_estimate": <2 for text/story, 3 for image, 5 for video>
        }}
      ]
    }}
  ]
}}

Rules:
- Exactly 7 days
- Each day has exactly {req.posts_per_day} post(s)
- Distribute across platforms: {platforms_str}
- Vary post types strategically for {req.goal}
- Use {req.tone} tone throughout all captions
- Write all captions in {req.language}
- Use realistic posting times for each platform
- If user has products, feature them naturally in relevant posts
- total_credits_estimate must equal the exact sum of all credits_estimate values"""

    # Use direct AI call with higher token limit for the full 7-day plan
    ai_text = ""
    s = store.get("settings", {})
    openai_key = (s.get("openai_key","") if s.get("openai_key") and "••••" not in s.get("openai_key","") else "") or os.getenv("OPENAI_API_KEY","")
    anthropic_key = (s.get("anthropic_key","") if s.get("anthropic_key") and "••••" not in s.get("anthropic_key","") else "") or os.getenv("ANTHROPIC_API_KEY","")
    if anthropic_key:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5", "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]},
                    timeout=60)
                r.raise_for_status()
                ai_text = r.json()["content"][0]["text"]
        except Exception:
            pass
    if not ai_text and openai_key:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}", "content-type": "application/json"},
                    json={"model": "gpt-4o-mini", "max_tokens": 4000,
                          "messages": [{"role": "system", "content": "You are a social media content strategist. Return ONLY valid JSON, no markdown."},
                                       {"role": "user", "content": prompt}]},
                    timeout=60)
                r.raise_for_status()
                ai_text = r.json()["choices"][0]["message"]["content"]
        except Exception:
            pass
    if not ai_text:
        raise HTTPException(status_code=503, detail="AI service unavailable. Please try again.")

    import re
    # Try direct parse first, then extract JSON block
    plan_data = None
    for attempt in [ai_text.strip(), None]:
        if attempt is not None:
            try:
                plan_data = json.loads(attempt)
                break
            except Exception:
                pass
        if plan_data is None:
            m = re.search(r'\{[\s\S]*\}', ai_text)
            if m:
                try:
                    plan_data = json.loads(m.group())
                    break
                except Exception:
                    pass
    if not plan_data:
        raise HTTPException(status_code=500, detail="AI did not return a valid plan. Please try again.")

    plan_id = uuid.uuid4().hex[:8]
    full_plan = {
        "plan_id": plan_id,
        "created_at": datetime.now().isoformat(),
        "niche": req.niche,
        "goal": req.goal,
        "posts_per_day": req.posts_per_day,
        "platforms": req.platforms,
        "tone": req.tone,
        "language": req.language,
        **plan_data,
    }

    plans = store.setdefault("content_plans", [])
    plans.insert(0, full_plan)
    store["content_plans"] = plans[:10]
    save_store()

    return full_plan


@router.get("/api/content/plans")
def get_content_plans(current_user: dict = Depends(get_current_user)):
    return {"plans": store.get("content_plans", [])}
