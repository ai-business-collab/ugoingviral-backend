"""
UgoingViral — Competitor Analysis
POST /api/competitor/analyze          → AI-powered competitor breakdown
POST /api/competitor/generate-similar → generate inspired content
"""
import json
from fastapi import APIRouter, Depends, Request, HTTPException
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()

_ESTIMATE_DISCLAIMER = (
    "AI estimate based on niche/platform norms — NOT live data from this "
    "account. We have no public API access to a competitor's private metrics, "
    "so follower/engagement figures are approximate guidance, not real numbers."
)


async def _call_ai(prompt: str, s: dict, max_tokens: int = 1500) -> str:
    import httpx
    if s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", "")):
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": s["anthropic_key"], "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["content"][0]["text"].strip()
    if s.get("openai_key") and "••••" not in str(s.get("openai_key", "")):
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {s['openai_key']}",
                         "Content-Type": "application/json"},
                json={"model": "gpt-4o-mini", "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=30,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    raise ValueError("no_ai_key")


@router.post("/api/competitor/analyze")
async def analyze_competitor(req: Request, current_user: dict = Depends(get_current_user)):
    from services.security import check_ai_rate_limit
    if not check_ai_rate_limit(current_user.get("id"), "competitor", 10, 60):
        raise HTTPException(429, "Too many competitor analyses — please wait a moment.")
    body = await req.json()
    handle   = str(body.get("handle", "")).strip().lstrip("@")
    platform = str(body.get("platform", "instagram")).lower()
    niche    = str(body.get("niche", "creator")).lower()
    if not handle:
        raise HTTPException(400, "handle is required")

    s = store.get("settings", {})
    has_ai = (s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", ""))) or \
             (s.get("openai_key") and "••••" not in str(s.get("openai_key", "")))

    result = None
    if has_ai:
        prompt = f"""You are a social media analytics expert. Generate a realistic competitor analysis for @{handle} on {platform} in the {niche} niche.

Return ONLY a valid JSON object:
{{
  "handle": "{handle}",
  "platform": "{platform}",
  "estimated_followers": <integer>,
  "posts_per_week": <float 1-14>,
  "avg_engagement_rate": <float 1.5-8.0>,
  "top_content_types": [
    {{"type": "Reels", "percentage": <int>}},
    {{"type": "Carousels", "percentage": <int>}},
    {{"type": "Static posts", "percentage": <int>}}
  ],
  "best_hashtags": ["<tag1>","<tag2>","<tag3>","<tag4>","<tag5>","<tag6>","<tag7>","<tag8>"],
  "best_posting_times": ["Mon HH:MM","Wed HH:MM","Fri HH:MM"],
  "content_themes": ["<theme1>","<theme2>","<theme3>","<theme4>"],
  "top_posts_preview": [
    {{"description": "<10-word post description>", "likes": <int>, "content_type": "reel|image|carousel"}},
    {{"description": "<10-word post description>", "likes": <int>, "content_type": "reel|image|carousel"}},
    {{"description": "<10-word post description>", "likes": <int>, "content_type": "reel|image|carousel"}}
  ],
  "strengths": ["<strength1>","<strength2>","<strength3>"],
  "gaps": ["<gap1>","<gap2>"]
}}
Return ONLY the JSON object, no markdown."""
        try:
            raw = await _call_ai(prompt, s)
            s2, e2 = raw.find("{"), raw.rfind("}") + 1
            if s2 >= 0 and e2 > 0:
                result = json.loads(raw[s2:e2])
        except Exception as ex:
            add_log(f"Competitor AI error: {ex}", "warn")

    # No fabricated fallback: if we couldn't produce a real AI analysis, say so
    # honestly instead of inventing follower/engagement numbers with an RNG.
    if not result:
        if not has_ai:
            return {"ok": False,
                    "detail": "No AI key configured — add an Anthropic or OpenAI key in Settings to get competitor analysis."}
        return {"ok": False,
                "detail": "Couldn't generate the analysis right now. Please try again."}

    result["handle"] = handle
    result["platform"] = platform
    # Every figure here is an AI approximation, not the account's real data.
    result["is_estimate"] = True
    result["disclaimer"] = _ESTIMATE_DISCLAIMER
    return {"ok": True, "analysis": result}


@router.post("/api/competitor/generate-similar")
async def generate_similar(req: Request, current_user: dict = Depends(get_current_user)):
    body     = await req.json()
    handle   = str(body.get("handle", "")).strip()
    themes   = body.get("themes", [])
    hashtags = body.get("hashtags", [])
    ctype    = str(body.get("top_content_type", "reel"))
    niche    = str(body.get("niche", "creator"))

    s = store.get("settings", {})
    brand_kit = store.get("brand_kit", {})
    brand_hint = f"Brand tagline: {brand_kit['tagline']}. " if brand_kit.get("tagline") else ""

    prompt = f"""Create 3 social media post ideas inspired by @{handle}'s successful content.
Top themes: {', '.join(themes[:3]) or 'engaging content'}
Content type: {ctype}
Niche: {niche}
{brand_hint}
Return ONLY a JSON array of 3 objects:
[
  {{
    "title": "<catchy title, max 8 words>",
    "caption": "<ready-to-post caption with emojis, 150-200 chars>",
    "hashtags": ["<tag1>","<tag2>","<tag3>","<tag4>","<tag5>"],
    "hook": "<scroll-stopping first line, max 12 words>",
    "content_type": "{ctype}"
  }}
]
Return ONLY the JSON array."""

    try:
        raw = await _call_ai(prompt, s, max_tokens=1200)
        s2, e2 = raw.find("["), raw.rfind("]") + 1
        posts = json.loads(raw[s2:e2]) if s2 >= 0 and e2 > 0 else []
        return {"ok": True, "posts": posts}
    except ValueError:
        return {"ok": False, "detail": "No AI key configured — add an Anthropic or OpenAI key in Settings"}
    except Exception as ex:
        add_log(f"Generate similar error: {ex}", "error")
        return {"ok": False, "detail": str(ex)}
