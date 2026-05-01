"""
UgoingViral — Viral Score Predictor
POST /api/viral-score  → predict viral potential 0-100 with tips
"""
import re, json
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()

PEAK_HOURS = {
    "instagram": {7, 8, 9, 11, 12, 17, 18, 19, 20},
    "tiktok":    {6, 7, 10, 11, 15, 19, 20, 21, 22},
    "youtube":   {9, 10, 11, 14, 15, 16, 17, 18},
    "twitter":   {8, 9, 12, 13, 17, 18},
    "facebook":  {9, 10, 11, 13, 14, 15, 16},
}

EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF]|[\U00002600-\U000027FF]|[\U0001F900-\U0001F9FF]"
)
CTA_WORDS = {"comment","share","save","follow","click","link","dm ","tag ","drop",
             "tell me","what do you","which","vote","reply","join","grab","get yours"}


def _rule_score(caption: str, hashtags: list, platform: str, post_time: str):
    score = 0
    tips  = []

    cap_len = len(caption)
    if 100 <= cap_len <= 220:
        score += 15
    elif cap_len < 60:
        score += 3
        tips.append("Caption is too short — aim for 100-220 characters for better engagement")
    elif cap_len > 400:
        score += 8
        tips.append("Caption may be too long — hook readers before the 'more' cutoff")
    else:
        score += 10

    tc = len(hashtags)
    if 8 <= tc <= 15:
        score += 15
    elif tc < 5:
        score += 5
        tips.append(f"Add more hashtags (you have {tc}, aim for 8-15) to boost discoverability")
    elif tc > 20:
        score += 8
        tips.append("Too many hashtags look spammy — trim to 8-15 focused tags")
    else:
        score += 12

    ec = len(EMOJI_RE.findall(caption))
    if 1 <= ec <= 6:
        score += 10
    elif ec == 0:
        tips.append("Add 2-4 emojis — they act as visual anchors that stop the scroll")
    else:
        score += 7
        tips.append("Too many emojis reduce readability — keep it to 2-6")

    cap_lower = caption.lower()
    has_cta = "?" in caption or any(w in cap_lower for w in CTA_WORDS)
    if has_cta:
        score += 12
    else:
        tips.append("Add a call-to-action — 'Save this', 'Comment below', or 'Tag a friend' lifts engagement")

    words = caption.split()
    hook_words = []
    for w in words:
        if any(p in w for p in [".", "!", "?", "\n"]):
            hook_words.append(w)
            break
        hook_words.append(w)
    hook_len = len(hook_words)
    if 5 <= hook_len <= 15:
        score += 10
    elif hook_len > 20:
        tips.append("Shorten your opening line to 5-15 words — it needs to stop the scroll instantly")
    else:
        score += 5

    try:
        if "T" in post_time:
            hour = int(post_time.split("T")[1].split(":")[0])
        elif ":" in post_time:
            hour = int(post_time.split(":")[0])
        else:
            hour = -1
        peak = PEAK_HOURS.get(platform, PEAK_HOURS["instagram"])
        if hour in peak:
            score += 12
        elif hour >= 0:
            closest = min(peak, key=lambda h: abs(h - hour))
            tips.append(f"Post at {closest:02d}:00 for peak {platform.capitalize()} engagement")
            score += 4
    except Exception:
        pass

    score = max(5, min(score, 93))

    if score >= 80:   grade, color, verdict = "A", "#10b981", "High viral potential!"
    elif score >= 65: grade, color, verdict = "B", "#f59e0b", "Good potential — small tweaks will push it further"
    elif score >= 45: grade, color, verdict = "C", "#ef4444", "Average — apply the tips below"
    else:             grade, color, verdict = "D", "#dc2626", "Needs improvement — apply the tips for much better results"

    return {"score": score, "grade": grade, "color": color, "verdict": verdict,
            "tips": tips[:4], "best_feature": None,
            "breakdown": {"caption_length": cap_len, "hashtag_count": tc,
                          "emoji_count": ec, "has_cta": has_cta}}


@router.post("/api/viral-score")
async def predict_viral_score(req: Request, current_user: dict = Depends(get_current_user)):
    body      = await req.json()
    caption   = str(body.get("caption", ""))
    hashtags  = body.get("hashtags", [])
    platform  = str(body.get("platform", "instagram")).lower()
    post_time = str(body.get("post_time", ""))

    if isinstance(hashtags, str):
        hashtags = [t.strip() for t in hashtags.split() if t.startswith("#")]

    result = _rule_score(caption, hashtags, platform, post_time)

    s = store.get("settings", {})
    has_ai = (s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", ""))) or \
             (s.get("openai_key") and "••••" not in str(s.get("openai_key", "")))

    if has_ai and caption:
        prompt = f"""Rate this social media post's viral potential for {platform}.

Caption: {caption[:500]}
Hashtags: {' '.join(hashtags[:15])}
Rule-based score: {result['score']}/100

Return ONLY JSON:
{{
  "adjusted_score": <int 0-100>,
  "ai_tip": "<one specific improvement tip, max 20 words>",
  "best_feature": "<what is strongest about this post, max 12 words>"
}}"""
        try:
            import httpx
            if s.get("anthropic_key") and "••••" not in str(s["anthropic_key"]):
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={"x-api-key": s["anthropic_key"],
                                 "anthropic-version": "2023-06-01",
                                 "content-type": "application/json"},
                        json={"model": "claude-haiku-4-5-20251001", "max_tokens": 250,
                              "messages": [{"role": "user", "content": prompt}]},
                        timeout=12,
                    )
                    r.raise_for_status()
                    raw = r.json()["content"][0]["text"].strip()
            else:
                async with httpx.AsyncClient() as c:
                    r = await c.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": f"Bearer {s['openai_key']}",
                                 "Content-Type": "application/json"},
                        json={"model": "gpt-4o-mini", "max_tokens": 250,
                              "messages": [{"role": "user", "content": prompt}]},
                        timeout=12,
                    )
                    r.raise_for_status()
                    raw = r.json()["choices"][0]["message"]["content"].strip()

            s2, e2 = raw.find("{"), raw.rfind("}") + 1
            if s2 >= 0 and e2 > 0:
                ai = json.loads(raw[s2:e2])
                adj = int(ai.get("adjusted_score", result["score"]))
                result["score"] = max(5, min(95, round((result["score"] + adj) / 2)))
                if ai.get("ai_tip"):
                    result["tips"].insert(0, ai["ai_tip"])
                    result["tips"] = result["tips"][:4]
                result["best_feature"] = ai.get("best_feature")
                sc = result["score"]
                if sc >= 80:   result["grade"], result["color"], result["verdict"] = "A", "#10b981", "High viral potential!"
                elif sc >= 65: result["grade"], result["color"], result["verdict"] = "B", "#f59e0b", "Good potential — small tweaks will push it further"
                elif sc >= 45: result["grade"], result["color"], result["verdict"] = "C", "#ef4444", "Average — apply the tips below"
                else:          result["grade"], result["color"], result["verdict"] = "D", "#dc2626", "Needs improvement"
        except Exception as ex:
            add_log(f"Viral score AI error: {ex}", "warn")

    return {"ok": True, **result}
