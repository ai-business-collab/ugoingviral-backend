import os, httpx, json, io, tempfile
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store

router = APIRouter()

CLAUDE_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
# Agent brain: OpenAI GPT-4o-mini (fast orchestration) → falls back to Claude Haiku
# Heavy content tasks always use Claude Sonnet
OPENAI_AGENT_MODEL  = "gpt-4o-mini"
CLAUDE_HEAVY_MODEL  = "claude-sonnet-4-6"
CLAUDE_LIGHT_MODEL  = "claude-haiku-4-5-20251001"
ELEVENLABS_VOICE    = "EXAVITQu4vr4xnSDxMaL"   # "Bella" — warm female voice


def _log_api_call(provider: str, model: str, action: str,
                  tokens_in: int = 0, tokens_out: int = 0):
    """Log provider API usage for the cost dashboard."""
    # Estimated cost per 1K tokens (USD)
    cost_table = {
        "gpt-4o-mini": (0.00015, 0.00060),
        "gpt-4o":      (0.00250, 0.01000),
        "claude-haiku-4-5-20251001":  (0.00025, 0.00125),
        "claude-sonnet-4-6": (0.00300, 0.01500),
        "whisper-1":   (0.006, 0),  # per minute, we store tokens as seconds
    }
    c_in, c_out = cost_table.get(model, (0.001, 0.002))
    cost_usd = (tokens_in / 1000) * c_in + (tokens_out / 1000) * c_out
    try:
        from services.store import store, save_store
        log = store.setdefault("provider_api_log", [])
        log.append({
            "provider": provider, "model": model, "action": action,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "cost_usd": round(cost_usd, 6),
            "ts": datetime.now().isoformat(),
        })
        store["provider_api_log"] = log[-500:]
        save_store()
    except Exception:
        pass

# ── Context builder ────────────────────────────────────────────────────────────

def _build_context(user_id: str, current_page: str = "") -> str:
    ustore = _load_user_store(user_id)
    billing = ustore.get("billing", store.get("billing", {}))
    plan = billing.get("plan", "free")
    credits = billing.get("credits", 50)

    from routes.billing import PLANS
    plan_info  = PLANS.get(plan, PLANS["free"])
    plan_name  = plan_info["name"]

    conns = store.get("connections", {})
    connected = [p for p, v in conns.items() if isinstance(v, dict) and v.get("username")]

    history_items = store.get("history", [])
    recent_content = len([h for h in history_items
                          if (datetime.now() - datetime.fromisoformat(h["timestamp"][:19])).days < 30
                          if "timestamp" in h]) if history_items else 0

    posts = store.get("scheduled_posts", [])
    pending_posts = len([p for p in posts if p.get("status") == "scheduled"])

    bonuses = billing.get("claimed_bonuses", [])
    auto_perm = ustore.get("agent_auto_permission", False)

    autopilot = ustore.get("autopilot", {})
    autopilot_active = autopilot.get("active", False)

    days_since_post: int | str = "never"
    if history_items:
        timestamps = []
        for h in history_items:
            if "timestamp" in h:
                try:
                    timestamps.append(datetime.fromisoformat(h["timestamp"][:19]))
                except Exception:
                    pass
        if timestamps:
            days_since_post = (datetime.now() - max(timestamps)).days

    ctx = f"""USER CONTEXT:
- Plan: {plan_name} ({credits} credits left)
- Connected platforms: {', '.join(connected) if connected else 'none'}
- Content created last 30 days: {recent_content}
- Pending scheduled posts: {pending_posts}
- Agent auto-permission: {'enabled' if auto_perm else 'disabled'}
- Autopilot: {'active' if autopilot_active else 'paused'}
- Days since last post: {days_since_post}
- Current page: {current_page or 'unknown'}
- User ID: {user_id}"""

    if plan in ("pro", "elite", "personal"):
        ctx += "\n- Support tier: Live support eligible"

    if plan == "agency":
        clients = ustore.get("client_accounts", [])
        ctx += f"\n- Agency clients: {len(clients)}"

    return ctx


def _build_system(plan: str, ctx: str, agent_name: str = "") -> str:
    role_desc = {
        "free":     "You are the UgoingViral AI assistant — friendly, helpful FAQ bot.",
        "starter":  "You are the UgoingViral AI assistant — friendly, helpful FAQ bot.",
        "basic":    "You are the UgoingViral AI assistant — friendly, helpful FAQ bot.",
        "pro":      "You are the UgoingViral Pro Support assistant. You provide direct, expert support.",
        "elite":    "You are the UgoingViral Elite Support assistant. You provide priority expert support.",
        "personal": "You are the user's dedicated Personal AI assistant on UgoingViral. Be highly personalized.",
    }.get(plan, "You are the UgoingViral AI assistant.")

    name_line = f"\nYour name is {agent_name}. Use your name naturally in conversation when it feels appropriate." if agent_name else ""

    return f"""{role_desc}{name_line}

{ctx}

PLATFORM FEATURES:
- Content Generator: captions, hooks, scripts, hashtags for IG/TikTok/YouTube/Facebook
- Content Pipeline: image → AI scripts → Runway video → ElevenLabs voice → final video
- Post & Schedule: calendar scheduling to multiple platforms simultaneously
- Automation: auto-like, auto-follow, auto-comment, engagement rules
- Billing: credits system, plans Free→Personal (0–2499 kr/mo)
- Connect: link social accounts (Instagram, TikTok, YouTube, Facebook, X, Telegram)
- Statistics: follower growth, views, likes, comments

CREDIT COSTS: Script 5cr · Image 8cr · Video 5s 20cr · Video 15s 40cr · Video 30s 80cr · Voice 15cr · Assembly 2cr · Post 1cr

RESPONSE RULES:
1. Detect the user's language and respond in THAT language (Danish if Danish, English if English, etc.)
2. Keep responses concise (max 3 short paragraphs). Be direct and actionable.
3. When you want to navigate the user to a section, add [[NAV:pagename]] at the END of your response.
   Valid pages: dashboard, generator, pipeline, history, content-library, library, products, posting, inbox, automation, connect, settings, billing, stats
4. When you want to suggest quick actions, add [[BTN:Label:action]] at the END. Actions: nav:pagename, generate, schedule
5. When asked to generate content directly, do it inline in your response.
6. If the user gives you AUTO PERMISSION and you detect stale content (>30 days, no recent posts), proactively suggest generating new content.
7. For Pro/Elite/Personal users who need human help: add [[ESCALATE]] to trigger live support.
8. If credits < 100, proactively mention the low balance and suggest topping up. [[NAV:billing]]
9. If days since last post > 3, proactively suggest creating new content. [[NAV:generator]]
10. If autopilot is paused and user has 200+ credits, proactively suggest resuming autopilot."""


# ── History helpers ────────────────────────────────────────────────────────────

def _load_history(user_id: str) -> list:
    ustore = _load_user_store(user_id)
    return ustore.get("agent_history", [])

def _save_history(user_id: str, history: list):
    ustore = _load_user_store(user_id)
    ustore["agent_history"] = history[-60:]
    _save_user_store(user_id, ustore)


# ── Models ─────────────────────────────────────────────────────────────────────

class AgentMessage(BaseModel):
    role: str
    content: str

class AgentRequest(BaseModel):
    message: str
    history: List[AgentMessage] = []
    context: str = ""
    voice_mode: bool = False


class AutoPermRequest(BaseModel):
    enabled: bool


# ── OpenAI call (agent brain — fast orchestration) ────────────────────────────

async def _call_openai(system: str, messages: list) -> str:
    if not OPENAI_API_KEY:
        return await _call_claude_light(system, messages)
    oai_msgs = [{"role": "system", "content": system}] + messages
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": OPENAI_AGENT_MODEL, "max_tokens": 600, "messages": oai_msgs},
        )
    if r.status_code != 200:
        # Fallback to Claude if OpenAI fails
        return await _call_claude_light(system, messages)
    data = r.json()
    usage = data.get("usage", {})
    _log_api_call("openai", OPENAI_AGENT_MODEL, "agent_chat",
                  usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
    return data["choices"][0]["message"]["content"]


# ── Claude call — light (fallback) ────────────────────────────────────────────

async def _call_claude_light(system: str, messages: list) -> str:
    if not CLAUDE_API_KEY:
        return "AI assistant is not configured. Please add OPENAI_API_KEY or ANTHROPIC_API_KEY."
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_LIGHT_MODEL, "max_tokens": 600,
                  "system": system, "messages": messages},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Claude API error {r.status_code}")
    data = r.json()
    usage = data.get("usage", {})
    _log_api_call("anthropic", CLAUDE_LIGHT_MODEL, "agent_chat",
                  usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    return data["content"][0]["text"]


# ── Claude call — heavy (content generation, scripts, video prompts) ──────────

async def _call_claude_heavy(system: str, messages: list, action: str = "content_generate") -> str:
    if not CLAUDE_API_KEY:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY ikke konfigureret")
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": CLAUDE_API_KEY,
                     "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_HEAVY_MODEL, "max_tokens": 2000,
                  "system": system, "messages": messages},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"Claude heavy API error {r.status_code}")
    data = r.json()
    usage = data.get("usage", {})
    _log_api_call("anthropic", CLAUDE_HEAVY_MODEL, action,
                  usage.get("input_tokens", 0), usage.get("output_tokens", 0))
    return data["content"][0]["text"]


# ── ElevenLabs TTS ────────────────────────────────────────────────────────────

async def _tts(text: str, voice_id: str = "") -> Optional[str]:
    if not ELEVENLABS_KEY:
        return None
    clean = text
    import re
    clean = re.sub(r'\[\[.*?\]\]', '', clean).strip()
    if not clean:
        return None
    vid = voice_id or ELEVENLABS_VOICE
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{vid}",
                headers={"xi-api-key": ELEVENLABS_KEY, "content-type": "application/json"},
                json={"text": clean[:800], "model_id": "eleven_multilingual_v2",
                      "voice_settings": {"stability": 0.5, "similarity_boost": 0.75}},
            )
        if r.status_code == 200:
            import uuid, os
            fn = f"/tmp/agent_voice_{uuid.uuid4().hex[:8]}.mp3"
            with open(fn, "wb") as f:
                f.write(r.content)
            _log_api_call("elevenlabs", "eleven_multilingual_v2", "agent_tts",
                          len(clean), 0)
            b64 = __import__('base64').b64encode(r.content).decode()
            return f"data:audio/mpeg;base64,{b64}"
    except Exception:
        pass
    return None


# ── Parse actions from response ───────────────────────────────────────────────

def _parse_actions(text: str) -> dict:
    import re
    actions = []
    nav_m = re.findall(r'\[\[NAV:(\w[\w-]*)\]\]', text)
    for page in nav_m:
        actions.append({"type": "navigate", "page": page})
    btn_m = re.findall(r'\[\[BTN:([^:]+):([^\]]+)\]\]', text)
    for label, act in btn_m:
        actions.append({"type": "button", "label": label, "action": act})
    escalate = bool(re.search(r'\[\[ESCALATE\]\]', text))
    clean = re.sub(r'\[\[.*?\]\]', '', text).strip()
    return {"text": clean, "actions": actions, "escalate": escalate}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/agent/chat")
async def agent_chat(req: AgentRequest, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    billing = ustore.get("billing", store.get("billing", {}))
    plan = billing.get("plan", "free")

    agent_name = ustore.get("agent_name", "")
    ctx = _build_context(uid, req.context)
    system = _build_system(plan, ctx, agent_name)

    # Build message list — use stored history + request history
    stored = _load_history(uid)
    all_msgs = stored[-12:] + [{"role": m.role, "content": m.content} for m in req.history[-4:]]
    all_msgs.append({"role": "user", "content": req.message})

    try:
        # OpenAI = agent orchestrator brain; Claude light as fallback
        raw = await _call_openai(system, all_msgs)
    except Exception as e:
        return {"response": f"Error: {str(e)[:120]}", "actions": [], "voice_url": None}

    parsed = _parse_actions(raw)

    # Save to history
    stored.append({"role": "user", "content": req.message})
    stored.append({"role": "assistant", "content": parsed["text"]})
    _save_history(uid, stored)

    # TTS — use voice if client requested it OR user preference is enabled
    voice_url = None
    voice_enabled = ustore.get("agent_voice_enabled", False)
    voice_id      = ustore.get("agent_voice_id", "")
    if (req.voice_mode or voice_enabled) and ELEVENLABS_KEY:
        voice_url = await _tts(parsed["text"], voice_id)

    return {
        "response":  parsed["text"],
        "actions":   parsed["actions"],
        "escalate":  parsed["escalate"],
        "voice_url": voice_url,
    }


@router.post("/api/agent/voice")
async def agent_voice(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Transcribe audio via OpenAI Whisper, then route through agent chat."""
    if not OPENAI_API_KEY and not CLAUDE_API_KEY:
        return {"response": "Not configured.", "voice_url": None}

    # Deduct 5 credits for voice message
    from services.store import save_store as _ss
    billing = store.setdefault("billing", {})
    credits = billing.get("credits", 0)
    if credits < 5:
        raise HTTPException(status_code=402, detail="Not enough credits for voice message (5 required)")
    billing["credits"] = credits - 5
    store["billing"] = billing
    _log_api_call("openai", "whisper-1", "voice_transcription_credit", 5, 0)
    _ss()

    transcript = ""
    if OPENAI_API_KEY:
        audio_bytes = await file.read()
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.openai.com/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                    files={"file": (file.filename or "audio.webm", audio_bytes, "audio/webm")},
                    data={"model": "whisper-1"},
                )
            if r.status_code == 200:
                transcript = r.json().get("text", "")
                _log_api_call("openai", "whisper-1", "voice_transcription",
                              len(audio_bytes) // 100, 0)
        except Exception:
            pass

    if not transcript:
        return {"response": "Kunne ikke transskribere lyden. Prøv at skrive i stedet.", "voice_url": None}

    # Run through chat with voice_mode=True
    req = AgentRequest(message=transcript, voice_mode=True)
    return await agent_chat(req, current_user)


@router.get("/api/agent/history")
def get_history(current_user: dict = Depends(get_current_user)):
    history = _load_history(current_user["id"])
    return {"history": history[-40:]}


@router.delete("/api/agent/history")
def clear_history(current_user: dict = Depends(get_current_user)):
    _save_history(current_user["id"], [])
    return {"ok": True}


@router.post("/api/agent/auto_permission")
def set_auto_permission(req: AutoPermRequest, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    ustore["agent_auto_permission"] = req.enabled
    _save_user_store(uid, ustore)
    return {"ok": True, "enabled": req.enabled}


@router.get("/api/agent/auto_permission")
def get_auto_permission(current_user: dict = Depends(get_current_user)):
    ustore = _load_user_store(current_user["id"])
    return {"enabled": ustore.get("agent_auto_permission", False)}


@router.get("/api/agent/name")
def get_agent_name(current_user: dict = Depends(get_current_user)):
    ustore = _load_user_store(current_user["id"])
    return {"name": ustore.get("agent_name", "")}


@router.patch("/api/agent/name")
async def set_agent_name(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    name = (d.get("name") or "").strip()[:30]
    ustore = _load_user_store(current_user["id"])
    ustore["agent_name"] = name
    _save_user_store(current_user["id"], ustore)
    return {"ok": True, "name": name}


@router.post("/api/agent/transcribe")
async def agent_transcribe(
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
):
    """Transcribe audio via Whisper — returns text only, costs 5 credits."""
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY not configured")

    billing = store.setdefault("billing", {})
    credits = billing.get("credits", 0)
    if credits < 5:
        raise HTTPException(status_code=402, detail="Not enough credits (5 required for transcription)")
    billing["credits"] = credits - 5
    store["billing"] = billing
    save_store()

    audio_bytes = await file.read()
    fname = file.filename or "audio.webm"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                files={"file": (fname, audio_bytes, file.content_type or "audio/webm")},
                data={"model": "whisper-1"},
            )
        if r.status_code == 200:
            transcript = r.json().get("text", "")
            _log_api_call("openai", "whisper-1", "agent_transcribe",
                          len(audio_bytes) // 100, 0)
            return {"transcript": transcript, "credits_used": 5,
                    "credits_left": billing["credits"]}
        raise HTTPException(status_code=500, detail=f"Whisper error {r.status_code}: {r.text[:200]}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)[:120]}")


@router.get("/api/agent/voices")
async def get_voices(current_user: dict = Depends(get_current_user)):
    """Return available ElevenLabs voices."""
    if not ELEVENLABS_KEY:
        return {"voices": []}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": ELEVENLABS_KEY},
            )
        if r.status_code == 200:
            raw = r.json().get("voices", [])
            voices = [{"id": v["voice_id"], "name": v["name"],
                       "category": v.get("category", "")} for v in raw]
            return {"voices": voices}
    except Exception:
        pass
    return {"voices": []}


@router.get("/api/agent/voice_settings")
def get_voice_settings(current_user: dict = Depends(get_current_user)):
    ustore = _load_user_store(current_user["id"])
    return {
        "voice_enabled": ustore.get("agent_voice_enabled", False),
        "voice_id":      ustore.get("agent_voice_id", ELEVENLABS_VOICE),
    }


@router.post("/api/agent/voice_settings")
async def save_voice_settings(req: Request, current_user: dict = Depends(get_current_user)):
    body = await req.json()
    ustore = _load_user_store(current_user["id"])
    if "voice_enabled" in body:
        ustore["agent_voice_enabled"] = bool(body["voice_enabled"])
    if "voice_id" in body:
        ustore["agent_voice_id"] = str(body["voice_id"])[:60]
    _save_user_store(current_user["id"], ustore)
    return {"ok": True}
