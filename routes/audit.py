"""
UgoingViral — Social Media Audit
POST /api/audit/analyze
"""
import json, hashlib
from fastapi import APIRouter, Depends, Request, HTTPException
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()

_GRADES = [(90, "A+", "#00e5ff"), (80, "A", "#22c55e"), (70, "B", "#84cc16"),
           (60, "C", "#f59e0b"), (50, "D", "#f97316"), (0, "F", "#ef4444")]


def _grade(score: int):
    for threshold, g, c in _GRADES:
        if score >= threshold:
            return g, c
    return "F", "#ef4444"


def _mock_audit(handle: str, platform: str, niche: str, bio: str,
                follower_range: str, posts_per_week: float) -> dict:
    seed = int(hashlib.md5(handle.encode()).hexdigest()[:6], 16)

    def jitter(base, spread=12):
        return min(100, max(0, base + (seed % spread) - spread // 2))

    # Profile completeness
    profile_score = 55
    profile_issues = []
    profile_fixes = []
    if not bio or len(bio) < 30:
        profile_issues.append("Bio is too short or missing — under 30 characters")
        profile_fixes.append("Write a 150-character bio that states who you help, how, and with a CTA")
    else:
        profile_score += 15
    if not bio or "link" not in bio.lower() and "http" not in bio.lower():
        profile_issues.append("No link or CTA detected in bio")
        profile_fixes.append("Add a link-in-bio URL and a short CTA like 'Grab the free guide ↓'")
    else:
        profile_score += 10
    if not handle or len(handle) > 20:
        profile_issues.append("Username is long — harder to remember and search")
        profile_fixes.append("Consider a shorter handle under 15 characters if available")
    else:
        profile_score += 10
    profile_score = jitter(min(profile_score, 100), 8)

    # Posting consistency
    consistency_score = 40
    consistency_issues = []
    consistency_fixes = []
    if posts_per_week < 1:
        consistency_issues.append("Posting less than once per week — algorithm deprioritizes inactive accounts")
        consistency_fixes.append("Commit to at least 3 posts per week with a content batch day")
    elif posts_per_week < 3:
        consistency_issues.append("Posting 1-2x/week — below algorithm threshold for significant reach")
        consistency_fixes.append("Increase to 4-5 posts/week; batch-create content every Sunday")
        consistency_score += 20
    elif posts_per_week < 5:
        consistency_fixes.append("You're posting consistently — consider scheduling at peak hours for +15% reach")
        consistency_score += 35
    else:
        consistency_fixes.append("Great posting frequency! Focus on quality and engagement rather than volume")
        consistency_score += 50
    consistency_score = jitter(min(consistency_score, 100), 10)

    # Content mix
    mix_score = 50
    mix_issues = []
    mix_fixes = []
    niche_lower = niche.lower()
    if "ecommerce" in niche_lower or "shop" in niche_lower:
        mix_issues.append("Product-only content has low organic reach on most platforms")
        mix_fixes.append("Follow the 80/20 rule: 80% value content, 20% promotional")
        mix_fixes.append("Add behind-the-scenes and customer testimonial content")
        mix_score += 15
    elif "fitness" in niche_lower:
        mix_fixes.append("Mix transformation posts, educational content, and motivational Reels equally")
        mix_fixes.append("Quick 15-second tips perform 3x better than long-form on TikTok and Reels")
        mix_score += 25
    elif "creator" in niche_lower or "personal" in niche_lower:
        mix_fixes.append("Alternate between educational, personal story, and entertainment content")
        mix_fixes.append("Polls and Q&As in Stories drive saves — a key ranking signal")
        mix_score += 20
    else:
        mix_issues.append("Content mix is unclear — diversify post types to maximize reach")
        mix_fixes.append("Use: 40% educational, 30% entertainment, 20% promotional, 10% personal")
    mix_score = jitter(min(mix_score, 100), 12)

    # Engagement potential
    engage_score = 45
    engage_issues = []
    engage_fixes = []
    fr = follower_range.lower()
    if "0-1" in fr or "1k" in fr.replace(" ", ""):
        engage_fixes.append("Under 1K: focus on community reply loops — reply to every comment within 1 hour")
        engage_fixes.append("Collaborate with 3 similar-sized creators this month for cross-pollination")
        engage_score += 20
    elif "10k" in fr or "50k" in fr:
        engage_issues.append("Mid-tier accounts often see declining engagement — algorithm tests at scale")
        engage_fixes.append("Run a 'comment to enter' giveaway to spike engagement signals")
        engage_fixes.append("Use interactive Stories stickers (polls, sliders) daily for 2 weeks")
        engage_score += 25
    else:
        engage_fixes.append("Focus on saves and shares — they outweigh likes in the algorithm at all follower levels")
        engage_score += 30
    engage_score = jitter(min(engage_score, 100), 10)

    overall = (profile_score + consistency_score + mix_score + engage_score) // 4
    grade, color = _grade(overall)

    verdicts = {
        "A+": "Exceptional — your account is optimized and growing efficiently.",
        "A":  "Strong account with minor improvements that could unlock the next level.",
        "B":  "Good foundation with clear gaps to address for faster growth.",
        "C":  "Average — several changes needed to compete in your niche.",
        "D":  "Below average — prioritize the quick wins below immediately.",
        "F":  "Critical issues found — follow the action plan to avoid stagnation.",
    }

    opportunities = []
    if consistency_score < 60:
        opportunities.append(f"Doubling post frequency to {max(3, int(posts_per_week)+2)}x/week could increase reach by 40-60% within 30 days")
    if profile_score < 70:
        opportunities.append("Optimizing your bio with a clear CTA typically lifts profile-to-follow conversion by 20-35%")
    if mix_score < 65:
        opportunities.append("Adding short-form video (Reels/Shorts) to your content mix can 3x organic impressions")
    opportunities.append(f"Accounts in the {niche} niche that post at peak hours see 25% higher engagement — use scheduling tools")
    if platform.lower() in ("instagram", "tiktok"):
        opportunities.append("Hashtag strategy optimization (tiered approach) can expand discovery reach by 2-4x")

    quick_wins = [
        "Pin your best-performing post to your profile today",
        "Add 3 relevant keywords to your bio for search discoverability",
        "Reply to every comment in the next 48 hours to boost your engagement rate",
        "Schedule your next 7 posts in advance to ensure consistency",
    ]

    return {
        "overall_score": overall,
        "grade": grade,
        "color": color,
        "verdict": verdicts.get(grade, "Room for improvement across the board."),
        "categories": [
            {"name": "Profile Completeness", "score": profile_score, "icon": "👤",
             "issues": profile_issues, "fixes": profile_fixes},
            {"name": "Posting Consistency", "score": consistency_score, "icon": "📅",
             "issues": consistency_issues, "fixes": consistency_fixes},
            {"name": "Content Mix", "score": mix_score, "icon": "🎨",
             "issues": mix_issues, "fixes": mix_fixes},
            {"name": "Engagement Potential", "score": engage_score, "icon": "💬",
             "issues": engage_issues, "fixes": engage_fixes},
        ],
        "top_opportunities": opportunities[:4],
        "quick_wins": quick_wins,
    }


async def _ai_audit(prompt: str, s: dict) -> str:
    import httpx
    if s.get("anthropic_key") and "••••" not in str(s.get("anthropic_key", "")):
        async with httpx.AsyncClient() as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": s["anthropic_key"],
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
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
                json={"model": "gpt-4o-mini", "max_tokens": 2000,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=45,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
    return ""


def _build_audit_prompt(handle, platform, niche, bio, follower_range, posts_per_week) -> str:
    return f"""You are a top social media growth strategist. Audit this {platform} account and return a JSON report.

Account: @{handle}
Platform: {platform}
Niche: {niche}
Bio: {bio or "(not provided)"}
Follower range: {follower_range}
Posts per week: {posts_per_week}

Return ONLY a valid JSON object:
{{
  "overall_score": <integer 0-100>,
  "grade": "<A+|A|B|C|D|F>",
  "color": "<hex color matching grade>",
  "verdict": "<2-sentence verdict on account health>",
  "categories": [
    {{
      "name": "Profile Completeness",
      "score": <0-100>,
      "icon": "👤",
      "issues": ["<specific issue>"],
      "fixes": ["<actionable fix>"]
    }},
    {{
      "name": "Posting Consistency",
      "score": <0-100>,
      "icon": "📅",
      "issues": ["<specific issue>"],
      "fixes": ["<actionable fix>"]
    }},
    {{
      "name": "Content Mix",
      "score": <0-100>,
      "icon": "🎨",
      "issues": ["<specific issue>"],
      "fixes": ["<actionable fix>"]
    }},
    {{
      "name": "Engagement Potential",
      "score": <0-100>,
      "icon": "💬",
      "issues": ["<specific issue>"],
      "fixes": ["<actionable fix>"]
    }}
  ],
  "top_opportunities": ["<high-impact growth opportunity>", "<opportunity>", "<opportunity>"],
  "quick_wins": ["<action doable today>", "<quick win>", "<quick win>", "<quick win>"]
}}

Rules:
- Be specific and actionable — no generic advice
- Scores should reflect actual weaknesses honestly
- Return ONLY the JSON object, no markdown"""


@router.post("/api/audit/analyze")
async def analyze_account(req: Request, current_user: dict = Depends(get_current_user)):
    body          = await req.json()
    handle        = str(body.get("handle", "")).strip().lstrip("@")
    platform      = str(body.get("platform", "instagram")).lower()
    niche         = str(body.get("niche", "creator"))
    bio           = str(body.get("bio", ""))[:500]
    follower_range = str(body.get("follower_range", "1K-10K"))
    posts_per_week = float(body.get("posts_per_week", 3))

    if not handle:
        raise HTTPException(400, "handle is required")

    s = store.get("settings", {})
    mock = _mock_audit(handle, platform, niche, bio, follower_range, posts_per_week)

    raw = await _ai_audit(
        _build_audit_prompt(handle, platform, niche, bio, follower_range, posts_per_week), s
    )
    if raw:
        s2, e2 = raw.find("{"), raw.rfind("}") + 1
        if s2 >= 0 and e2 > 0:
            try:
                mock = json.loads(raw[s2:e2])
            except Exception:
                pass

    add_log(f"📋 Audit: @{handle} on {platform}", "success")
    return {"ok": True, "audit": mock,
            "meta": {"handle": handle, "platform": platform, "niche": niche}}
