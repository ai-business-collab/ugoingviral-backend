"""
UgoingViral — AI Caption Improver
POST /api/caption/improve  → rewrite caption per improvement type
"""
from fastapi import APIRouter, Depends, Request, HTTPException
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()

_PROMPTS = {
    "engaging":     "Make this much more engaging and emotionally resonant. Add a hook, storytelling element, or relatable moment. Keep a similar length.",
    "professional": "Rewrite as polished, authoritative, and professional. Remove slang. Sound like a market leader.",
    "shorter":      "Condense to the single most impactful version. Aim for under 100 characters. Keep the core message.",
    "cta":          "Rewrite with a strong, specific call-to-action that drives immediate engagement (comment, save, share, click, or buy).",
    "viral":        "Rewrite to maximize viral sharing potential. Use a curiosity gap, bold claim, or trending hook. Make it impossible NOT to read.",
}


@router.post("/api/caption/improve")
async def improve_caption(req: Request, current_user: dict = Depends(get_current_user)):
    body = await req.json()
    original   = str(body.get("caption", "")).strip()
    imp_type   = str(body.get("improvement_type", "engaging")).lower()
    niche      = str(body.get("niche", "general"))
    platform   = str(body.get("platform", "instagram"))

    if not original:
        raise HTTPException(400, "caption is required")

    instruction = _PROMPTS.get(imp_type, _PROMPTS["engaging"])

    prompt = f"""You are an elite social media copywriter. {instruction}

Platform: {platform}
Niche: {niche}

Original caption:
{original}

Return ONLY the improved caption — no explanation, no quotes, no markdown."""

    s = store.get("settings", {})
    improved = None

    try:
        import httpx
        if s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", "")):
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": s["anthropic_key"],
                             "anthropic-version": "2023-06-01",
                             "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=20,
                )
                r.raise_for_status()
                improved = r.json()["content"][0]["text"].strip()
        elif s.get("openai_key") and "••••" not in str(s.get("openai_key", "")):
            async with httpx.AsyncClient() as c:
                r = await c.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {s['openai_key']}",
                             "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "max_tokens": 400,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=20,
                )
                r.raise_for_status()
                improved = r.json()["choices"][0]["message"]["content"].strip()
        else:
            raise HTTPException(422, "No AI key configured — add your Anthropic or OpenAI key in Settings")
    except HTTPException:
        raise
    except Exception as ex:
        add_log(f"Caption improver error: {ex}", "error")
        raise HTTPException(500, str(ex))

    return {"ok": True, "original": original, "improved": improved, "type": imp_type}
