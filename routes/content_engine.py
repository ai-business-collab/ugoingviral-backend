"""
UgoingViral — Content Engine Route
POST /api/content-engine/generate  → 30-day AI content calendar
GET  /api/content-engine/calendar  → load saved calendar
POST /api/content-engine/save-idea → mark idea as used / generate post from it
"""
import json
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store, add_log

router = APIRouter()

BEST_TIMES = {
    "monday":    ["07:00","12:00","18:00"],
    "tuesday":   ["09:00","13:00","19:00"],
    "wednesday": ["08:00","12:00","17:00"],
    "thursday":  ["09:00","14:00","18:00"],
    "friday":    ["08:00","11:00","17:00"],
    "saturday":  ["10:00","13:00","20:00"],
    "sunday":    ["11:00","14:00","19:00"],
}

DAYS_OF_WEEK = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

NICHE_CONTEXTS = {
    "ecommerce":   "online store selling physical products, focus on product showcases, deals, customer testimonials",
    "creator":     "personal brand content creator, focus on tutorials, behind-the-scenes, personal stories, viral trends",
    "agency":      "marketing/creative agency, focus on case studies, industry tips, team culture, client results",
    "restaurant":  "food & beverage business, focus on dishes, ambience, chef stories, specials, reservations",
    "fitness":     "fitness & wellness brand, focus on workouts, nutrition tips, transformations, motivation, community",
    "saas":        "SaaS/tech product, focus on features, customer stories, how-to tips, industry insights",
    "other":       "business/brand, focus on value, expertise, community engagement",
}

async def _call_ai(prompt: str) -> str:
    s = store.get("settings", {})
    if s.get("anthropic_key") and "••••" not in str(s["anthropic_key"]):
        try:
            import httpx
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": s["anthropic_key"], "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 4096,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=45,
                )
                r.raise_for_status()
                return r.json()["content"][0]["text"]
        except Exception as e:
            add_log(f"Anthropic error: {e}", "error")
    if s.get("openai_key") and "••••" not in str(s["openai_key"]):
        try:
            import httpx
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {s['openai_key']}",
                             "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "max_tokens": 4096,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=45,
                )
                r.raise_for_status()
                return r.json()["choices"][0]["message"]["content"]
        except Exception as e:
            add_log(f"OpenAI error: {e}", "error")
    from fastapi import HTTPException
    raise HTTPException(status_code=422, detail="No AI key configured — add your Anthropic or OpenAI key in Settings → API Keys")


@router.post("/api/content-engine/generate")
async def generate_calendar(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    niche       = d.get("niche", "other").lower()
    tone        = d.get("tone", "engaging and friendly")
    audience    = d.get("target_audience", "general audience")
    platforms   = d.get("platforms", ["instagram"])
    num_days    = min(int(d.get("days", 30)), 30)
    platform_str = ", ".join(platforms) if platforms else "instagram"
    niche_ctx   = NICHE_CONTEXTS.get(niche, NICHE_CONTEXTS["other"])

    prompt = f"""You are an expert social media strategist. Create a {num_days}-day content calendar.

Business type: {niche_ctx}
Target audience: {audience}
Tone: {tone}
Platforms: {platform_str}

Return ONLY a valid JSON array of exactly {num_days} objects. Each object must have these exact keys:
{{
  "day": <integer 1-{num_days}>,
  "title": "<short catchy idea title, max 60 chars>",
  "caption": "<ready-to-post caption with emojis, 100-200 chars>",
  "hashtags": ["<tag1>","<tag2>","<tag3>","<tag4>","<tag5>","<tag6>","<tag7>","<tag8>","<tag9>","<tag10>"],
  "best_time": "<HH:MM>",
  "content_type": "<one of: image|video|carousel|story|reel>",
  "hook": "<scroll-stopping first line, max 15 words>"
}}

Make captions varied: mix educational, entertaining, promotional, user-generated, behind-the-scenes content.
Return ONLY the JSON array, no markdown, no explanation."""

    try:
        raw = await _call_ai(prompt)
        # Extract JSON array from response
        raw = raw.strip()
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            raise ValueError("No JSON array in response")
        ideas = json.loads(raw[start:end])
        if not isinstance(ideas, list) or len(ideas) == 0:
            raise ValueError("Empty ideas list")
        # Ensure all fields exist
        for i, idea in enumerate(ideas):
            idea.setdefault("day", i + 1)
            idea.setdefault("title", f"Day {i+1} content")
            idea.setdefault("caption", "")
            idea.setdefault("hashtags", [])
            idea.setdefault("best_time", BEST_TIMES[DAYS_OF_WEEK[i % 7]][i % 3])
            idea.setdefault("content_type", "image")
            idea.setdefault("hook", idea["title"])
            idea["used"] = False
    except Exception as e:
        add_log(f"Content Engine generation error: {e}", "error")
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=str(e))

    calendar_data = {
        "generated_at": datetime.utcnow().isoformat(),
        "niche": niche,
        "tone": tone,
        "audience": audience,
        "platforms": platforms,
        "ideas": ideas,
    }
    store["content_calendar"] = calendar_data
    save_store()
    add_log(f"📅 Content calendar generated: {len(ideas)} ideas for {niche}", "success")
    return {"ok": True, "ideas": ideas, "count": len(ideas)}


@router.get("/api/content-engine/calendar")
def get_calendar(current_user: dict = Depends(get_current_user)):
    cal = store.get("content_calendar")
    if not cal:
        return {"ok": False, "ideas": [], "meta": None}
    return {"ok": True, "ideas": cal.get("ideas", []), "meta": {
        "generated_at": cal.get("generated_at"),
        "niche":        cal.get("niche"),
        "tone":         cal.get("tone"),
        "audience":     cal.get("audience"),
    }}


@router.patch("/api/content-engine/idea/{day}")
async def update_idea(day: int, req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    cal = store.get("content_calendar", {})
    ideas = cal.get("ideas", [])
    for idea in ideas:
        if idea.get("day") == day:
            idea.update({k: v for k, v in d.items() if k != "day"})
            break
    store["content_calendar"] = cal
    save_store()
    return {"ok": True}
