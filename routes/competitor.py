"""
UgoingViral — Competitor Analysis
POST /api/competitor/analyze          → AI-powered competitor breakdown
POST /api/competitor/generate-similar → generate inspired content
"""
import json, random
from fastapi import APIRouter, Depends, Request, HTTPException
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()


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


def _mock_analysis(handle: str, platform: str, niche: str) -> dict:
    rng = random.Random(sum(ord(c) for c in handle + platform))
    pct_reels = rng.randint(40, 65)
    pct_car   = rng.randint(20, min(35, 100 - pct_reels))
    pct_img   = 100 - pct_reels - pct_car
    return {
        "handle": handle, "platform": platform,
        "estimated_followers": rng.randint(5000, 250000),
        "posts_per_week": round(rng.uniform(3, 10), 1),
        "avg_engagement_rate": round(rng.uniform(2.0, 6.5), 1),
        "top_content_types": [
            {"type": "Reels", "percentage": pct_reels},
            {"type": "Carousels", "percentage": pct_car},
            {"type": "Static posts", "percentage": pct_img},
        ],
        "best_hashtags": [f"#{handle}", f"#{niche}", "#viral", "#trending",
                          "#content", "#creator", "#socialmedia", "#growth"],
        "best_posting_times": ["Mon 09:00", "Wed 18:00", "Fri 12:00"],
        "content_themes": ["Tutorials", "Behind the scenes", "Product showcases", "Trending sounds"],
        "top_posts_preview": [
            {"description": "Tutorial showing step-by-step process",
             "likes": rng.randint(1000, 15000), "content_type": "reel"},
            {"description": "Before & after transformation reveal",
             "likes": rng.randint(800, 12000), "content_type": "carousel"},
            {"description": "Day in the life vlog-style content",
             "likes": rng.randint(600, 10000), "content_type": "reel"},
        ],
        "strengths": ["Consistent posting schedule", "Strong visual identity", "High-quality Reels"],
        "gaps": ["Limited use of carousels", "Inconsistent caption length"],
    }


@router.post("/api/competitor/analyze")
async def analyze_competitor(req: Request, current_user: dict = Depends(get_current_user)):
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

    if not result:
        result = _mock_analysis(handle, platform, niche)

    result["handle"] = handle
    result["platform"] = platform
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
