"""
UgoingViral — AI Video Script Generator
POST /api/video-script/generate
"""
import json
from fastapi import APIRouter, Depends, Request, HTTPException
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()

# scenes per duration bucket
_SCENE_COUNTS = {15: 1, 30: 3, 60: 5, 90: 7}


async def _call_ai(prompt: str, s: dict) -> str:
    import httpx
    if s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", "")):
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": s["anthropic_key"],
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2500,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=45,
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
    if s.get("openai_key") and "••••" not in str(s.get("openai_key", "")):
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {s['openai_key']}",
                         "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "max_tokens": 2500,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=45,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    raise HTTPException(422, "No AI key configured — add your Anthropic or OpenAI key in Settings")


def _build_prompt(topic: str, niche: str, duration: int, style: str,
                  platform: str, template_hint: str = "") -> str:
    n_scenes = _SCENE_COUNTS.get(duration, 3)
    hook_end = 3
    cta_start = duration - 3
    if n_scenes > 0:
        scene_span = (cta_start - hook_end) // n_scenes
        scene_times = [f"{hook_end + i*scene_span}–{hook_end + (i+1)*scene_span}s"
                       for i in range(n_scenes)]
    else:
        scene_times = ["3–12s"]

    tmpl_note = f"\nBase template structure to follow:\n{template_hint}\n" if template_hint else ""

    return f"""You are an elite {platform} video script writer.
Create a {duration}-second {style} video script.

Topic: {topic}
Niche: {niche}
Platform: {platform}{tmpl_note}

Return ONLY a valid JSON object:
{{
  "title": "<scroll-stopping video title>",
  "hook": {{
    "text": "<exact spoken words for 0–3s — must instantly grab attention, max 15 words>",
    "action": "<what the creator does on camera>",
    "text_overlay": "<text shown on screen>"
  }},
  "scenes": [{", ".join([f'''{{
    "id": {i+1},
    "time_range": "{scene_times[i]}",
    "title": "<scene title>",
    "script": "<exact spoken words — natural conversational speech>",
    "action": "<camera direction or action>",
    "text_overlay": "<on-screen text>",
    "b_roll": "<visual suggestion>"
  }}''' for i in range(n_scenes)])}],
  "cta": {{
    "text": "<exact spoken CTA — specific and action-driving>",
    "time_range": "{cta_start}–{duration}s",
    "overlay": "<text on screen>",
    "action": "<visual action>"
  }},
  "music_mood": "<suggested music genre and energy>",
  "total_words": <estimated spoken word count as integer>,
  "pro_tips": ["<production tip>", "<filming tip>", "<editing tip>"]
}}

Rules:
- Hook MUST stop the scroll: use curiosity gap, bold claim, or relatable pain point
- Script text should be natural spoken language at ~130 words/minute pace
- Keep each scene script to fit its time range
- Return ONLY the JSON object, no markdown"""


@router.post("/api/video-script/generate")
async def generate_script(req: Request, current_user: dict = Depends(get_current_user)):
    body          = await req.json()
    topic         = str(body.get("topic", "")).strip()
    niche         = str(body.get("niche", "creator"))
    duration      = min(max(int(body.get("duration", 30)), 15), 90)
    style         = str(body.get("style", "educational"))
    platform      = str(body.get("platform", "tiktok"))
    template_hint = str(body.get("template_hint", ""))

    if not topic:
        raise HTTPException(400, "topic is required")

    s   = store.get("settings", {})
    raw = await _call_ai(_build_prompt(topic, niche, duration, style, platform, template_hint), s)

    s2, e2 = raw.find("{"), raw.rfind("}") + 1
    if s2 < 0 or e2 <= 0:
        raise HTTPException(500, "AI returned invalid response")
    try:
        script = json.loads(raw[s2:e2])
    except Exception as ex:
        raise HTTPException(500, f"Parse error: {ex}")

    add_log(f"🎬 Script generated: {topic[:40]}", "success")
    return {"ok": True, "script": script,
            "meta": {"topic": topic, "duration": duration, "style": style, "platform": platform}}
