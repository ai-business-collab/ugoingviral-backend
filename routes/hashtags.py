"""
UgoingViral — Hashtag Research
POST /api/hashtags/research       → AI hashtag research (trending/medium/niche)
GET  /api/hashtags/sets           → list saved sets
POST /api/hashtags/sets           → save a set
DELETE /api/hashtags/sets/{sid}   → delete saved set
"""
import json, uuid
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store, add_log

router = APIRouter()


def _fallback(topic: str, platform: str) -> dict:
    slug = topic.lower().replace(" ", "")
    words = [w.strip().lstrip("#") for w in topic.lower().split() if len(w.strip()) > 2]
    base  = [f"#{w}" for w in words[:5]]
    plat  = {
        "instagram": ["#instagram", "#instagood", "#reels", "#explore", "#contentcreator"],
        "tiktok":    ["#fyp", "#foryou", "#tiktoktrend", "#viral", "#tiktok"],
        "youtube":   ["#youtube", "#shorts", "#subscribe", "#youtuber", "#video"],
    }.get(platform, ["#viral", "#trending", "#content", "#socialmedia", "#digital"])
    return {
        "trending": {
            "hashtags": (plat + base)[:10],
            "reach": "1M–10M per tag", "competition": "Very High",
            "description": "Mass-reach tags used by top creators — great for visibility",
        },
        "medium": {
            "hashtags": [f"#{slug}tips", f"#{slug}daily", f"#{slug}life",
                         f"#{slug}content"] + base + ["#contentmarketing", "#socialmedia"],
            "reach": "100K–1M per tag", "competition": "Medium",
            "description": "Balanced reach with better targeting",
        },
        "niche": {
            "hashtags": [f"#{slug}community", f"#{slug}lover", f"#{slug}goals",
                         f"#{slug}inspo"] + base[:3] + ["#niche", "#smallbiz"],
            "reach": "10K–100K per tag", "competition": "Low",
            "description": "Highly targeted tags to reach your ideal audience",
        },
    }


@router.post("/api/hashtags/research")
async def research_hashtags(req: Request, current_user: dict = Depends(get_current_user)):
    body     = await req.json()
    topic    = str(body.get("topic", "")).strip()
    platform = str(body.get("platform", "instagram")).lower()
    niche    = str(body.get("niche", "general")).lower()

    if not topic:
        from fastapi import HTTPException
        raise HTTPException(400, "topic is required")

    s = store.get("settings", {})
    has_ai = (s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", ""))) or \
             (s.get("openai_key") and "••••" not in str(s.get("openai_key", "")))

    result = None
    if has_ai:
        prompt = f"""You are a social media hashtag expert. Research hashtags for "{topic}" on {platform} ({niche} niche).

Return ONLY a valid JSON object:
{{
  "trending": {{
    "hashtags": ["<10 high-reach tags, no spaces, with #>"],
    "reach": "<estimated reach range, e.g. 1M-10M per tag>",
    "competition": "Very High",
    "description": "<one sentence about when to use these>"
  }},
  "medium": {{
    "hashtags": ["<10 medium-reach tags, with #>"],
    "reach": "<e.g. 100K-1M per tag>",
    "competition": "Medium",
    "description": "<one sentence>"
  }},
  "niche": {{
    "hashtags": ["<10 niche-specific tags, with #>"],
    "reach": "<e.g. 10K-100K per tag>",
    "competition": "Low",
    "description": "<one sentence>"
  }}
}}
Make tags realistic, specific, and varied. Include topic-specific tags, not just generic ones.
Return ONLY the JSON object."""
        try:
            import httpx
            if s.get("anthropic_key") and "••••" not in str(s["anthropic_key"]):
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": s["anthropic_key"],
                                 "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 1000,
                              "messages": [{"role": "user", "content": prompt}]},
                        timeout=25,
                    )
                    r.raise_for_status()
                    raw = r.json()["content"][0]["text"].strip()
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {s['openai_key']}",
                                 "Content-Type": "application/json"},
                        json={"model": "gpt-4o-mini", "max_tokens": 1000,
                              "messages": [{"role": "user", "content": prompt}]},
                        timeout=25,
                    )
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()
            s2, e2 = raw.find("{"), raw.rfind("}") + 1
            if s2 >= 0 and e2 > 0:
                result = json.loads(raw[s2:e2])
        except Exception as ex:
            add_log(f"Hashtag research AI error: {ex}", "warn")

    if not result:
        result = _fallback(topic, platform)

    return {"ok": True, "topic": topic, "platform": platform, **result}


@router.get("/api/hashtags/sets")
def get_hashtag_sets(current_user: dict = Depends(get_current_user)):
    return {"sets": store.get("hashtag_sets", [])}


@router.post("/api/hashtags/sets")
async def save_hashtag_set(req: Request, current_user: dict = Depends(get_current_user)):
    body = await req.json()
    name = str(body.get("name", "")).strip()
    tags = body.get("tags", [])
    if not name or not tags:
        from fastapi import HTTPException
        raise HTTPException(400, "name and tags are required")
    entry = {
        "id": uuid.uuid4().hex[:10],
        "name": name[:80],
        "tags": [t for t in tags if isinstance(t, str)][:30],
        "saved_at": datetime.utcnow().isoformat(),
    }
    sets = store.get("hashtag_sets", [])
    sets.append(entry)
    store["hashtag_sets"] = sets[-50:]
    save_store()
    return {"ok": True, "set": entry}


@router.delete("/api/hashtags/sets/{sid}")
def delete_hashtag_set(sid: str, current_user: dict = Depends(get_current_user)):
    sets = [s for s in store.get("hashtag_sets", []) if s.get("id") != sid]
    store["hashtag_sets"] = sets
    save_store()
    return {"ok": True}
