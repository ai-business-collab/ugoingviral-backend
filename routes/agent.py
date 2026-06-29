import os, httpx, json, io, tempfile, re as _re, uuid as _uuid
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store
from services.security import limiter

router = APIRouter()

CLAUDE_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
OPENAI_API_KEY   = os.getenv("OPENAI_API_KEY", "")
ELEVENLABS_KEY   = os.getenv("ELEVENLABS_API_KEY", "")
# Agent brain: OpenAI GPT-4o-mini (fast orchestration) → falls back to Claude Haiku
# Heavy content tasks always use Claude Sonnet
OPENAI_AGENT_MODEL  = "gpt-4o-mini"
CLAUDE_HEAVY_MODEL  = "claude-sonnet-4-6"
CLAUDE_LIGHT_MODEL  = "claude-haiku-4-5-20251001"
# User-facing agent voice catalog. Two curated options the user can switch
# between via chat ("change voice" / "skift stemme") or in Settings.
VOICE_OPTIONS = {
    "nova": {"id": "lcMyyd2HUfFzxdCaC4Ta", "name": "Nova", "description": "Warm & friendly female"},
    "max":  {"id": "y0s2ExEMuum3muUnA6Zd", "name": "Max",  "description": "Professional & clear male"},
}
DEFAULT_VOICE_KEY = "nova"
# Default fallback when a user has not selected a voice. Female default = Nova.
ELEVENLABS_VOICE    = VOICE_OPTIONS[DEFAULT_VOICE_KEY]["id"]


def _voice_key_from_id(voice_id: str) -> str:
    """Reverse lookup: ElevenLabs voice_id -> our short key, or empty."""
    for k, v in VOICE_OPTIONS.items():
        if v["id"] == voice_id:
            return k
    return ""


# Detects "change voice" intent across DA + EN. Conservative — only fires on
# clearly-voice-related phrases so we don't hijack normal chat about audio.
_VOICE_INTENT_PATTERNS = (
    "change voice", "switch voice", "choose voice", "pick voice",
    "select voice", "different voice", "another voice",
    "skift stemme", "skifte stemme", "byt stemme", "vælg stemme",
    "anden stemme", "ny stemme",
)


def _detect_voice_intent(text: str) -> bool:
    if not text:
        return False
    low = text.lower().strip()
    return any(p in low for p in _VOICE_INTENT_PATTERNS)

# ── Agent background task config ──────────────────────────────────────────────
TASK_LIMITS = {"free": 0, "starter": 2, "pro": 5, "agency": -1}

_TASK_TYPE_MAP = {
    "monitor_hashtag":     {"schedule": "daily",  "label": "Monitor hashtag"},
    "engage_followers":    {"schedule": "daily",  "label": "Engage followers"},
    "post_content":        {"schedule": "daily",  "label": "Auto-post content"},
    "analyze_competitors": {"schedule": "weekly", "label": "Competitor analysis"},
}

_MONITOR_RE    = _re.compile(r"\b(monitor|watch|follow|track)\s+#?(\w+)", _re.I)
_ENGAGE_RE     = _re.compile(r"\b(engage|interact).{0,25}(follow|audience|fans|supporters)", _re.I)
_POST_RE       = _re.compile(r"\b(post|publish|share).{0,25}(auto|automat|schedul|regular|daily|for me)", _re.I)
_COMPETITOR_RE = _re.compile(r"\b(analyz|track|monitor).{0,25}(compet|rival)", _re.I)


def _detect_task_intent(msg: str):
    """Return (task_type, params) if a background-task intent is found, else None."""
    m = _MONITOR_RE.search(msg)
    if m:
        return "monitor_hashtag", {"hashtag": m.group(2).lower().lstrip("#")}
    if _ENGAGE_RE.search(msg):
        return "engage_followers", {}
    if _POST_RE.search(msg):
        return "post_content", {}
    if _COMPETITOR_RE.search(msg):
        return "analyze_competitors", {}
    return None


def _create_agent_task(uid: str, task_type: str, params: dict) -> dict:
    task = {
        "id":         _uuid.uuid4().hex[:12],
        "type":       task_type,
        "label":      _TASK_TYPE_MAP.get(task_type, {}).get("label", task_type),
        "params":     params,
        "schedule":   _TASK_TYPE_MAP.get(task_type, {}).get("schedule", "daily"),
        "active":     True,
        "created_at": datetime.now().isoformat(),
        "last_run":   None,
        "run_count":  0,
    }
    ustore = _load_user_store(uid)
    tasks  = ustore.setdefault("agent_tasks", [])
    tasks.append(task)
    ustore["agent_tasks"] = tasks
    _save_user_store(uid, ustore)
    return task


def _real_follower_snapshot(uid: str) -> dict:
    """Real follower counts from the user's already-synced platform analytics
    (services run_analytics_sync refreshes these from the real platform APIs).
    Returns only platforms that are genuinely connected and reported a count."""
    ustore = _load_user_store(uid)
    pa = ustore.get("platform_analytics", {})
    out: dict = {}
    for name, d in pa.items():
        if isinstance(d, dict) and d.get("connected") and not d.get("error"):
            c = d.get("followers", d.get("subscribers"))
            if isinstance(c, (int, float)):
                out[name] = c
    return out


def _run_agent_task_real(uid: str, task: dict) -> dict:
    """Produce an HONEST result for a background task using real data only.

    UgoingViral does NOT perform automated likes/follows/comments, so we never
    claim fabricated engagement ("gained X followers", "liked Y posts"). Where
    real data exists we report it; otherwise we say there's nothing to report.
    """
    t = task.get("type", "")

    if t in ("monitor_hashtag", "engage_followers"):
        # No real engagement engine — report a real analytics snapshot instead.
        counts = _real_follower_snapshot(uid)
        if counts:
            parts = ", ".join(f"{p}: {c:,} followers" for p, c in counts.items())
            return {
                "description": f"Analytics snapshot — {parts}",
                "follower_counts": counts,
                "note": "Automated engagement is not performed; this is a real analytics snapshot, not actions taken.",
            }
        return {
            "description": "No connected platform analytics yet — nothing to report.",
            "follower_counts": {},
            "note": "Connect a platform under Connect to get real analytics.",
        }

    if t == "post_content":
        # Report the real number of posts the user has scheduled for today.
        ustore = _load_user_store(uid)
        today  = datetime.now().strftime("%Y-%m-%d")
        n = sum(1 for p in ustore.get("scheduled_posts", [])
                if str(p.get("scheduled_time", "")).startswith(today))
        if n:
            return {"description": f"{n} post{'s' if n != 1 else ''} scheduled for today",
                    "posts_scheduled": n}
        return {"description": "No posts scheduled for today yet", "posts_scheduled": 0}

    if t == "analyze_competitors":
        return {
            "description": "Competitor analysis runs on-demand — open the Competitor tool for a real breakdown.",
            "note": "No automated competitor tracking is performed.",
        }

    return {"description": "Task completed"}


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
    # Also feed the global cost tracker (per-user attribution via ContextVar) so
    # the admin cost dashboard sees agent chat / TTS / transcription spend too.
    try:
        from services import api_tracker
        if provider in ("openai", "anthropic"):
            api_tracker.track(provider, "tokens", (tokens_in or 0) + (tokens_out or 0), feature=action)
        elif provider == "elevenlabs":
            api_tracker.track("elevenlabs", "chars", tokens_in or 0, feature=action)
    except Exception:
        pass

# ── Content performance insights ──────────────────────────────────────────────

def _get_content_insights(user_id: str) -> dict:
    """Analyse content_performance to discover what works best for this user."""
    ustore = _load_user_store(user_id)
    perf   = [p for p in ustore.get("content_performance", [])
               if p.get("engagement_rate") is not None]
    if not perf:
        return {}

    sorted_desc = sorted(perf, key=lambda x: x.get("engagement_rate", 0), reverse=True)
    top3   = sorted_desc[:3]
    worst3 = sorted_desc[-3:]

    # Average per platform
    plat_er: dict = {}
    for p in perf:
        pl = p.get("platform", "")
        if pl:
            plat_er.setdefault(pl, []).append(p.get("engagement_rate", 0))
    best_platform = max(plat_er, key=lambda k: sum(plat_er[k]) / len(plat_er[k])) if plat_er else None

    # Average per content type
    type_er: dict = {}
    for p in perf:
        ct = p.get("content_type", "")
        if ct:
            type_er.setdefault(ct, []).append(p.get("engagement_rate", 0))
    best_content_type = max(type_er, key=lambda k: sum(type_er[k]) / len(type_er[k])) if type_er else None

    # Average per posting hour
    hour_er: dict = {}
    for p in perf:
        ts = p.get("posted_at", "")
        try:
            hour = int(ts[11:13]) if len(ts) >= 13 else -1
            if 0 <= hour < 24:
                hour_er.setdefault(hour, []).append(p.get("engagement_rate", 0))
        except Exception:
            pass
    best_hour = max(hour_er, key=lambda h: sum(hour_er[h]) / len(hour_er[h])) if hour_er else None

    avg_er = round(sum(p.get("engagement_rate", 0) for p in perf) / len(perf), 4) if perf else 0.0

    return {
        "top_posts": [
            {"platform": p.get("platform"), "content_type": p.get("content_type"),
             "engagement_rate": p.get("engagement_rate"),
             "caption_snippet": (p.get("caption") or "")[:80]}
            for p in top3
        ],
        "worst_posts": [
            {"platform": p.get("platform"), "engagement_rate": p.get("engagement_rate")}
            for p in worst3
        ],
        "best_platform":      best_platform,
        "best_content_type":  best_content_type,
        "best_posting_hour":  best_hour,
        "total_tracked":      len(perf),
        "avg_engagement_rate": avg_er,
    }



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

    # ── Content performance insights ──────────────────────────────────────────
    insights = _get_content_insights(user_id)
    if insights.get("total_tracked", 0) > 0:
        avg_pct = round(insights["avg_engagement_rate"] * 100, 1)
        ctx += (
            f"\n\nCONTENT PERFORMANCE INSIGHTS ({insights['total_tracked']} posts tracked):"
        )
        if insights.get("best_platform"):
            ctx += f"\n- Best performing platform: {insights['best_platform']}"
        if insights.get("best_content_type"):
            ctx += f"\n- Best content type: {insights['best_content_type']}"
        if insights.get("best_posting_hour") is not None:
            ctx += f"\n- Best posting hour: {insights['best_posting_hour']:02d}:00"
        ctx += f"\n- Average engagement rate: {avg_pct}%"
        if insights.get("top_posts"):
            t = insights["top_posts"][0]
            er_pct = round((t.get("engagement_rate") or 0) * 100, 1)
            ctx += (
                f"\n- Top post: {t.get('content_type','')} on {t.get('platform','')}"
                f" ({er_pct}% ER) — \"{t.get('caption_snippet','')}...\""
            )
    else:
        # Fallback: use platform defaults from OPTIMAL_TIMES
        from routes.growth import OPTIMAL_TIMES
        connected_platforms = [p for p, v in
                               store.get("connections", {}).items()
                               if isinstance(v, dict) and v.get("username")]
        if connected_platforms:
            main_plat = connected_platforms[0]
            best_times = OPTIMAL_TIMES.get(main_plat, ["09:00", "13:00", "18:00"])
            ctx += f"\n- Recommended posting times ({main_plat}): {', '.join(best_times)}"
            ctx += "\n- No personal performance data yet — track your posts to get personalised insights"

    # ── NIE cross-platform insights ───────────────────────────────────────────
    try:
        import httpx as _hx
        _conns = store.get("connections", {})
        _plats = [p for p, v in _conns.items() if isinstance(v, dict) and v.get("username")]
        if _plats:
            _nie_plat = _plats[0]
            _auto = store.get("automation", {})
            _nie_niche = _auto.get("niche", "")
            _r = _hx.get(
                "http://localhost:4000/api/nie/insights",
                params={"platform": _nie_plat, "niche": _nie_niche, "days": 30},
                timeout=2,
            )
            if _r.status_code == 200:
                _ni = _r.json()
                if _ni.get("sample_size", 0) >= 10:
                    ctx += (
                        f"\n\nNIE CROSS-PLATFORM INSIGHTS (from {_ni['sample_size']} similar accounts):"
                        f"\n- Best content type for {_nie_plat}: {_ni.get('best_content_type','image')}"
                        f"\n- Best posting hours: {', '.join(str(h) + ':00' for h in _ni.get('best_posting_hours', []))}"
                    )
                    _cats = _ni.get('top_hashtag_categories', [])
                    if _cats:
                        ctx += f"\n- Top hashtag categories: {', '.join(_cats[:3])}"
    except Exception:
        pass

    # ── GROWTH GUIDANCE — connect Account Audit + Content Team to the agent ────
    # So the agent knows WHAT the user should improve and can proactively help.
    try:
        ctx += _growth_guidance_block(ustore)
    except Exception:
        pass

    return ctx


def _growth_guidance_block(ustore: dict) -> str:
    """A compact block injected into the agent context from the user's latest
    Account Audit + Content Team brief, so the agent can reference concrete
    recommendations and offer to act on them. Kept short for token budget."""
    out = ""
    audit = ustore.get("last_audit") or {}
    if audit:
        out += "\n\nACCOUNT AUDIT (latest — use to guide the user):"
        if audit.get("grade") is not None:
            out += f"\n- Overall: grade {audit.get('grade')} ({audit.get('overall_score','?')}/100) for @{audit.get('handle','')} on {audit.get('platform','')}"
        issues = []
        for cat in (audit.get("categories") or []):
            for iss in (cat.get("issues") or [])[:1]:
                issues.append(f"{cat.get('name','')}: {iss}")
        if issues:
            out += "\n- Top issues: " + " | ".join(issues[:3])
        opps = audit.get("top_opportunities") or []
        if opps:
            out += "\n- Opportunities: " + " | ".join(o for o in opps[:2])
        wins = audit.get("quick_wins") or []
        if wins:
            out += "\n- Quick wins: " + " | ".join(w for w in wins[:2])
        out += ("\n- PROACTIVELY surface the single most impactful issue and offer to help fix it. "
                "You can directly apply in-app changes (niche, posting cadence, CTA, quiet hours) with the "
                "user's confirmation. For platform bios (TikTok/Instagram) you can't edit them via API — "
                "guide the user step-by-step instead.")
    else:
        out += ("\n\nACCOUNT AUDIT: none run yet. If the user wants to grow or improve their profile, "
                "suggest running an Account Audit ([[NAV:audit]]) to get concrete recommendations.")

    ct = ustore.get("content_team") or {}
    brief = (ct.get("weekly_brief") or "").strip()
    if brief:
        snippet = brief.replace("\n", " ")[:280]
        out += f"\n\nCONTENT TEAM (latest weekly brief): {snippet}…"
        out += "\n- You can point the user to the Content Team ([[NAV:content-team]]) to run/approve their plan."
    return out


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
- Video Studio: AI video generation (Quick Video, Hyper Realistic, Premium Animation) + upload your own clips. A "✂️ Smart Cutting" feature (auto-cut video to the best moments) is coming soon.
- Content Library & Media Library: browse, organise and reuse every generated/uploaded video, image and audio file. Each item has ⚡ Post Now and 📅 Schedule buttons.
- Post & Schedule: calendar scheduling to multiple platforms simultaneously
- Automation: auto-like, auto-follow, auto-comment, engagement rules
- Billing: credits system, plans Free→Personal (0–2499 kr/mo)
- Connect: link social accounts (Instagram, TikTok, YouTube, Facebook, X, Telegram)
- Statistics: follower growth, views, likes, comments

PLATFORM CAPTION LIMITS (characters; emojis count as ~2):
- TikTok video: 2200 (the caption is the video title)
- TikTok photo: title ~90 (auto-trimmed for you) + description up to 4000 — long captions go in the description automatically
- Instagram: 2200
- X / Twitter: 280
- YouTube: title 100, description 5000
The Content Generator automatically keeps captions within the selected platform's limit, and Post & Schedule shows a live character counter per platform that warns when you go over. When a TikTok photo caption is long, it is placed in the description field, so it will still post.

FOLDER SYSTEM (Content Library + Media Library):
- Folders are UNIFIED and shared between the Content Library and the Media Library — a folder created in one appears in the other.
- To create a folder: click "📁 New Folder". To move an item into a folder: drag it onto a folder tab, or use the "Move to…" dropdown on the item.
- To delete a custom folder: press the ✕ on its tab (or right-click it) in the Content Library, or pick it in the Media Library dropdown and press "🗑️ Delete folder". Deleting a folder keeps the items — they just become un-filed. Default folders (Videos, Images, Audio, Posts, Uploads) cannot be deleted.

CREDIT COSTS: Script 5cr · Image 8cr · Video 5s 20cr · Video 15s 40cr · Video 30s 80cr · Voice 15cr · Assembly 2cr · Post 1cr

RESPONSE RULES:
1. LANGUAGE: Detect the user's language from their CURRENT message and reply in the SAME language. Danish → Danish, English → English, German → German, Spanish → Spanish, etc. Match the user every turn — if they switch language, switch with them. If their message is too short or ambiguous to tell (e.g. just "ok", a name, or a single emoji), ask: "What language would you like me to speak — English or Danish?" / "Hvilket sprog vil du have jeg taler — engelsk eller dansk?". Never mix languages within a single reply.
2. Keep responses concise (max 3 short paragraphs). Be direct and actionable.
3. When you want to navigate the user to a section, add [[NAV:pagename]] at the END of your response.
   Valid pages: dashboard, generator, pipeline, history, content-library, library, products, posting, inbox, automation, connect, settings, billing, stats, audit, content-team
4. When you want to suggest quick actions, add [[BTN:Label:action]] at the END. Actions: nav:pagename, generate, schedule
5. When asked to generate content directly, do it inline in your response.
6. If the user gives you AUTO PERMISSION and you detect stale content (>30 days, no recent posts), proactively suggest generating new content.
7. For Pro/Elite/Personal users who need human help: add [[ESCALATE]] to trigger live support.
8. If credits < 100, proactively mention the low balance and suggest topping up. [[NAV:billing]]
9. If days since last post > 3, proactively suggest creating new content. [[NAV:generator]]
10. If autopilot is paused and user has 200+ credits, proactively suggest resuming autopilot.
11. GROWTH COACH: take the user by the hand. When ACCOUNT AUDIT or CONTENT TEAM data is in your context, reference it: name the single most impactful profile issue and OFFER to help fix it (e.g. "Your bio is missing a CTA — want me to help you add one?"). For improving/growing/strategy questions, route to the Account Audit ([[NAV:audit]]) or Content Team ([[NAV:content-team]]).
12. ACT ON SETTINGS: the app can apply these in-app changes — niche, posting cadence (posts per day), CTA, quiet hours. To do it, state the exact change and ask the user to confirm with "Yes"; the APP (not you) performs and confirms it. NEVER state that a setting has already been changed — you don't apply it yourself, so don't claim success. For things that can't be changed via API (TikTok/Instagram bio, display name), give clear step-by-step instructions for the user — never claim you changed them."""


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


# ── OpenAI call — heavy (content generation: ideas, scripts) ──────────────────
# Mirrors _call_claude_heavy but on the OpenAI key. RAISES on any failure so the
# caller can cleanly fall back (e.g. Content Team -> template ideas/scripts).
async def _call_openai_heavy(system: str, messages: list,
                             action: str = "content_generate",
                             max_tokens: int = 1500) -> str:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY ikke konfigureret")
    oai_msgs = [{"role": "system", "content": system}] + messages
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                     "Content-Type": "application/json"},
            json={"model": OPENAI_AGENT_MODEL, "max_tokens": max_tokens, "messages": oai_msgs},
        )
    if r.status_code != 200:
        raise HTTPException(status_code=500, detail=f"OpenAI error {r.status_code}")
    data = r.json()
    usage = data.get("usage", {})
    _log_api_call("openai", OPENAI_AGENT_MODEL, action,
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


# ── UgoingViral action intent-router ──────────────────────────────────────────
# Deterministically detect when the user is asking the agent to DO something on
# UgoingViral (post / schedule / generate / show stats) and actually call the
# right internal endpoint, instead of only chatting. Scope is strictly
# UgoingViral actions — nothing external. Posting to a real social account is
# ALWAYS gated behind an explicit "Skal jeg poste nu? Ja/Nej" confirmation
# (stored as a pending action between messages).

_PLATFORMS = {
    "tiktok":    ("tiktok", "tik tok"),
    "instagram": ("instagram", "insta", "ig"),
    "youtube":   ("youtube", "yt"),
    "facebook":  ("facebook", "fb"),
    "twitter":   ("twitter", "x.com", " x ", "tweet"),
}

_AFFIRM = {"ja", "ja tak", "jep", "jo", "yes", "yep", "yeah", "ok", "okay", "okay ja",
           "gør det", "goer det", "post nu", "post den", "post det", "publicer",
           "udgiv", "send", "do it", "go", "post it", "yes please", "ja gør det"}
_NEGATE = {"nej", "nej tak", "no", "nope", "stop", "annuller", "annullér", "cancel",
           "vent", "ikke endnu", "not now", "drop det"}

_POST_VERB_RE  = _re.compile(r"\b(post|poste|publicer|publicér|udgiv|udgive|slå\s+op|slaa\s+op|publish|share\s+it)\b", _re.I)
_CREATE_VERB_RE = _re.compile(r"\b(lav|skriv|opret|generer|generér|create|write|make|draft)\b", _re.I)
_SCHEDULE_RE   = _re.compile(r"\b(planl[æa]g|planlaeg|schedule|sæt\s+op|saet\s+op)\b", _re.I)
_STATS_RE      = _re.compile(r"\b(statistik\w*|stats|analytics|mine\s+tal|resultater|hvordan\s+(klarer|går)|performance|insights?)\b", _re.I)
_NUM_RE        = _re.compile(r"\b(\d{1,2})\b")
_DA_HINTS = ("æ", "ø", "å", "opslag", "planl", "vis", "mine", "lav", " og ", " til ", "uge", "statistik", "skriv")

# ── Settings the agent can change (with the user's confirmation) ──────────────
_NICHE_RE    = _re.compile(r"\b(?:set|change|update|skift|ændr|aendr|sæt|saet)\b.{0,20}\b(?:niche|nische)\b\s*(?:to|til|=|:)?\s*(.+)", _re.I)
_CADENCE_RE  = _re.compile(r"\b(\d{1,2})\s*(?:posts?|opslag|gange|times|x)\b[^.\n]{0,14}?\b(?:per|a|an|om|pr\.?|each|/)?\s*(?:day|dag|dagen|daily|dgl)\b", _re.I)
_CADENCE_RE2 = _re.compile(r"\b(?:cadence|frequency|frekvens|post(?:ing)?\s*(?:rate|frequency))\b[^.\n]{0,14}?(\d{1,2})", _re.I)
_CADENCE_RE3 = _re.compile(r"\b(\d{1,2})\s*/\s*day\b", _re.I)
_CTA_RE      = _re.compile(r"\b(?:cta|call[\s-]?to[\s-]?action)\b\s*(?:to|til|=|:)?\s*(.+)", _re.I)
_BIO_RE      = _re.compile(r"\b(?:update|change|fix|edit|skift|ændr|aendr|opdater|rediger)\b.{0,16}\bbio(?:graphy|grafi)?\b|\bbio\b.{0,16}\b(?:cta|link|update|change)\b", _re.I)


def _apply_user_setting(ustore: dict, key: str, value, da: bool = True) -> tuple:
    """Apply an in-app setting change the user confirmed. Returns (ok, message).
    The message is localized to the user's language (`da`) so the whole reply
    reads in ONE language. Shared by the chat-confirm flow and the
    /api/agent/apply_setting endpoint."""
    key = (key or "").lower().strip()
    settings = ustore.setdefault("settings", {})
    auto = ustore.setdefault("automation", {})
    if key == "niche":
        v = str(value or "").strip().strip('"\'.').strip()[:80]
        if not v:
            return False, ("Niche må ikke være tom." if da else "Niche can't be empty.")
        settings["niche"] = v
        auto["niche"] = v
        return True, (f"niche sat til “{v}”" if da else f"niche set to “{v}”")
    if key in ("posts_per_day", "cadence"):
        try:
            n = max(1, min(10, int(value)))
        except Exception:
            return False, ("Angiv et antal opslag pr. dag (1–10)." if da
                           else "Give a number of posts per day (1–10).")
        try:
            from routes.autopilot import _spread_times
            times = _spread_times(n)
        except Exception:
            times = ["09:00", "14:00", "18:00"][:n] or ["12:00"]
        auto["posts_per_day"] = n
        auto["post_times"] = times
        return True, (f"posting-frekvens sat til {n}/dag ({', '.join(times)})" if da
                      else f"posting cadence set to {n}/day ({', '.join(times)})")
    if key == "cta":
        v = str(value or "").strip().strip('"\'.').strip()[:120]
        if not v:
            return False, ("CTA må ikke være tom." if da else "CTA can't be empty.")
        settings["preferred_cta"] = v
        return True, (f"foretrukken CTA gemt: “{v}” (jeg bruger den når jeg skriver dine opslag)" if da
                      else f"preferred CTA saved: “{v}” (I'll use it when drafting your posts)")
    if key == "quiet_hours":
        try:
            start, end = value
            auto["quiet_start"] = str(start)[:5]
            auto["quiet_end"] = str(end)[:5]
            auto["respect_quiet_hours"] = True
            return True, (f"stilletimer sat til {auto['quiet_start']}–{auto['quiet_end']}" if da
                          else f"quiet hours set to {auto['quiet_start']}–{auto['quiet_end']}")
        except Exception:
            return False, ("Angiv stilletimer som start og slut (fx 22:00–07:00)." if da
                           else "Give quiet hours as start and end (e.g. 22:00–07:00).")
    return False, ("Den indstilling kan ikke ændres automatisk endnu." if da
                   else "That setting can't be changed automatically yet.")


def _bio_guidance(da: bool) -> str:
    """Step-by-step bio guidance — the agent cannot edit a TikTok/IG bio via API."""
    if da:
        return ("Jeg kan desværre ikke ændre din bio direkte (platformene tillader ikke bio-redigering via API). "
                "Men her er en stærk opskrift — det tager 1 minut:\n"
                "1. Hvem du hjælper + hvordan (fx “Jeg hjælper travle ejere med at vokse på TikTok”).\n"
                "2. Et bevis eller en niche-detalje.\n"
                "3. En tydelig CTA + link (fx “Gratis guide ↓”).\n"
                "Sådan opdaterer du den: åbn appen (TikTok/Instagram) → Profil → Rediger profil → Bio → indsæt teksten → Gem. "
                "Vil du have at jeg skriver et par bio-forslag du kan kopiere?")
    return ("I can't edit your bio directly (TikTok/Instagram don't allow bio edits via API). "
            "But here's a strong formula — takes a minute:\n"
            "1. Who you help + how (e.g. “I help busy founders grow on TikTok”).\n"
            "2. A proof point or niche detail.\n"
            "3. A clear CTA + link (e.g. “Free guide ↓”).\n"
            "To update it: open the app (TikTok/Instagram) → Profile → Edit profile → Bio → paste → Save. "
            "Want me to write a few copy-paste bio options for you?")


def _is_danish(msg: str) -> bool:
    low = (msg or "").lower()
    if any(h in low for h in _DA_HINTS):
        return True
    return True  # DK-first product — default Danish when ambiguous


def _detect_platform(msg: str) -> str:
    low = f" {(msg or '').lower()} "
    for plat, aliases in _PLATFORMS.items():
        if any(a in low for a in aliases):
            return plat
    return ""


def _extract_topic(msg: str) -> str:
    """Pull the subject of a post out of phrases like 'lav et opslag om X …'."""
    m = _re.search(r"\bom\s+(.+)", msg, _re.I) or _re.search(r"\babout\s+(.+)", msg, _re.I)
    topic = (m.group(1) if m else msg).strip()
    # Trim trailing "og post det til tiktok" / "and post it to tiktok"
    topic = _re.split(r"\b(og|and|,)\b\s*(post|poste|publicer|publish|udgiv|slå|share)", topic, flags=_re.I)[0].strip()
    for plat, aliases in _PLATFORMS.items():
        for a in aliases:
            topic = _re.sub(_re.escape(a), "", topic, flags=_re.I)
    topic = _re.sub(r"\b(til|to|på|paa|for)\b\s*$", "", topic.strip(), flags=_re.I).strip(" .,-")
    return topic[:200]


async def _gen_caption(topic: str, platform: str, language: str) -> str:
    """Generate one caption via the existing content engine (real AI call)."""
    from routes.content import _call_ai, _build_prompt, _inject_preferred_cta
    from models import ContentRequest
    try:
        req = ContentRequest(content_type="caption", platform=platform or "instagram",
                             product_title=topic or "", product_description=topic or "",
                             language=language, tone="engaging")
        caption = (await _call_ai(_build_prompt(req), language)).strip()
        # Honor the user's saved preferred CTA in agent-drafted captions too.
        return _inject_preferred_cta(caption, req)
    except Exception:
        # Fallback so scheduling/posting still works if the model call fails.
        return (f"🔥 {topic}" if topic else "🔥 Nyt opslag fra UgoingViral")


def _log_agent_activity(uid: str, ustore: dict, description: str, kind: str, details: dict = None):
    log = ustore.setdefault("agent_activity_log", [])
    log.append({"ts": datetime.now().isoformat(), "day": datetime.now().strftime("%Y-%m-%d"),
                "task_type": kind, "description": description, "details": details or {}})
    ustore["agent_activity_log"] = log[-200:]


def _agent_reply(uid: str, ustore: dict, user_msg: str, text: str, actions=None, save_store_now=True):
    """Persist chat history + (optionally) the store, return the chat payload."""
    hist = ustore.setdefault("agent_history", [])
    hist.append({"role": "user", "content": user_msg})
    hist.append({"role": "assistant", "content": text})
    ustore["agent_history"] = hist[-60:]
    if save_store_now:
        _save_user_store(uid, ustore)
    return {"response": text, "actions": actions or [], "escalate": False, "voice_url": None}


class _FakeReq:
    """Minimal stand-in for fastapi Request so we can call publish_via_api()
    directly with a synthesized JSON body."""
    def __init__(self, data: dict):
        self._data = data
    async def json(self):
        return self._data


async def _execute_post_now(uid: str, ustore: dict, current_user: dict, pending: dict) -> dict:
    """Actually publish via the real posting endpoint and report the result."""
    from routes.posts import publish_via_api
    platform = pending.get("platform", "")
    content  = pending.get("content", "")
    da = pending.get("da", True)
    try:
        result = await publish_via_api(
            _FakeReq({"platform": platform, "content": content,
                      "image_url": pending.get("image_url"),
                      "video_url": pending.get("video_url")}),
            current_user,
        )
    except Exception as e:
        result = {"status": "error", "message": str(e)[:160]}
    status = result.get("status", "error")
    url = result.get("post_url") or result.get("url") or ""
    ustore.pop("agent_pending_action", None)
    if status in ("published", "processing"):
        _log_agent_activity(uid, ustore, f"Posted to {platform}", "post_content", {"platform": platform, "url": url})
        msg = (f"✅ Opslaget er postet til {platform.title()}!" if da else f"✅ Posted to {platform.title()}!")
        if url:
            msg += f" {url}"
    else:
        why = result.get("message", "ukendt fejl")
        msg = (f"⚠️ Kunne ikke poste til {platform.title()}: {why}. "
               f"Tjek at {platform.title()} er forbundet under Connect." if da
               else f"⚠️ Couldn't post to {platform.title()}: {why}. Make sure {platform.title()} is connected under Connect.")
    return _agent_reply(uid, ustore, pending.get("origin_msg", ""), msg,
                        actions=[{"type": "navigate", "page": "connect"}] if status not in ("published", "processing") else [])


def _niche_topic(ustore: dict) -> str:
    return (ustore.get("settings", {}).get("niche")
            or (ustore.get("automation") or {}).get("niche")
            or "your niche")


async def _youtube_video_worker(uid: str, current_user: dict, topic: str, da: bool):
    """Background: text → AI image → video → upload to YouTube. Runs detached so
    the chat reply returns immediately (generation takes a couple of minutes).
    Charges credits only on a successful video; notifies the user when done."""
    import asyncio as _a
    try:
        from services.ai_media import generate_text_to_video
        from routes.posts import publish_via_api
        # Build a concrete IMAGE scene + MOTION brief from the topic + the user's
        # real brand signals — never send the raw topic to the image/video model.
        from services.visual_prompt import build_visual_prompts, build_brand_context, NEGATIVE_PROMPT
        try:
            from routes.content_team import _collect_signals
            brand = build_brand_context(_collect_signals(uid))
        except Exception:
            brand = {}
        own = list(brand.get("own_images") or [])
        v = await build_visual_prompts({"hook": topic}, brand)
        res = await generate_text_to_video(
            uid=uid, duration=5, aspect_ratio="9:16",
            image_prompt=v["image_prompt"], motion_prompt=v["motion_prompt"],
            negative_prompt=v.get("negative_prompt", NEGATIVE_PROMPT),
            start_image_url=(own[0] if own else ""), brand=brand, item={"hook": topic})
        ustore = _load_user_store(uid)
        if not res.get("ok") or not res.get("video_url"):
            _log_agent_activity(uid, ustore, "YouTube video generation failed", "post_content",
                                {"platform": "youtube", "error": res.get("message", "")})
            _save_user_store(uid, ustore)
            _notify(uid, "youtube_failed", "YouTube video failed",
                    res.get("message", "Video generation failed — no credits charged."))
            return
        # Charge for the generated media (image 8 + clip 40), then upload.
        try:
            from routes.content_team import IMAGE_COST, VIDEO_COST
            b = ustore.setdefault("billing", {})
            b["credits"] = max(0, (b.get("credits", 0) or 0) - (IMAGE_COST + VIDEO_COST))
            _save_user_store(uid, ustore)
        except Exception:
            pass
        caption = await _gen_caption(topic, "youtube", "da" if da else "en")
        result = await publish_via_api(
            _FakeReq({"platform": "youtube", "content": caption,
                      "title": (topic or "New video")[:90], "video_url": res["video_url"]}),
            current_user,
        )
        ustore = _load_user_store(uid)
        ok = result.get("status") in ("published", "processing")
        url = result.get("post_url") or result.get("url") or ""
        _log_agent_activity(uid, ustore, "Uploaded YouTube video" if ok else "YouTube upload failed",
                            "post_content", {"platform": "youtube", "url": url, "msg": result.get("message", "")})
        _save_user_store(uid, ustore)
        _notify(uid, "youtube_done" if ok else "youtube_failed",
                "YouTube video uploaded" if ok else "YouTube upload failed",
                (f"Your video about “{topic}” is live. {url}" if ok
                 else f"Upload failed: {result.get('message','')}. The video was generated — try Connect/retry."))
    except Exception as e:
        try:
            _notify(uid, "youtube_failed", "YouTube video error", str(e)[:160])
        except Exception:
            pass


def _notify(uid: str, kind: str, title: str, body: str):
    try:
        from routes.notifications import push_notification
        push_notification(uid, kind, title, body)
    except Exception:
        pass


async def _execute_youtube_video(uid: str, ustore: dict, current_user: dict, pending: dict) -> dict:
    """Kick off YouTube video generation+upload in the background and reply now."""
    import asyncio as _a
    da = pending.get("da", True)
    topic = pending.get("topic", "") or _niche_topic(ustore)
    ustore.pop("agent_pending_action", None)
    # Credit pre-check (image + clip).
    try:
        from routes.content_team import IMAGE_COST, VIDEO_COST
        need = IMAGE_COST + VIDEO_COST
        have = (ustore.get("billing", {}) or {}).get("credits", 0) or 0
        if have < need:
            txt = (f"Du har ikke nok credits ({have}/{need}) til en YouTube-video." if da
                   else f"Not enough credits ({have}/{need}) for a YouTube video.")
            return _agent_reply(uid, ustore, pending.get("origin_msg", ""), txt,
                                actions=[{"type": "navigate", "page": "billing"}])
    except Exception:
        pass
    _log_agent_activity(uid, ustore, f"Generating YouTube video about {topic}", "post_content",
                        {"platform": "youtube", "status": "processing"})
    _a.create_task(_youtube_video_worker(uid, current_user, topic, da))
    txt = (f"🎬 Jeg laver din YouTube-video om “{topic}” nu — den uploades om et par minutter. "
           f"Jeg giver besked når den er klar." if da
           else f"🎬 Generating your YouTube video about “{topic}” now — it'll upload in a couple of minutes. "
                f"I'll notify you when it's live.")
    return _agent_reply(uid, ustore, pending.get("origin_msg", ""), txt)


async def _route_ugv_action(uid: str, message: str, ustore: dict, current_user: dict):
    """Return a chat payload if this message is a UgoingViral action, else None."""
    msg = (message or "").strip()
    low = msg.lower()
    da = _is_danish(msg)

    # 0) Resolve a pending confirmation first.
    pending = ustore.get("agent_pending_action")
    if pending and pending.get("type") in ("post_now", "youtube_video", "apply_setting"):
        if low in _AFFIRM or any(low.startswith(a) for a in _AFFIRM):
            if pending.get("type") == "youtube_video":
                return await _execute_youtube_video(uid, ustore, current_user, pending)
            if pending.get("type") == "apply_setting":
                ustore.pop("agent_pending_action", None)
                ok, m = _apply_user_setting(ustore, pending.get("key"), pending.get("value"),
                                            da=bool(pending.get("da", da)))
                _save_user_store(uid, ustore)
                if ok:
                    txt = (f"✅ Klaret — {m}." if da else f"✅ Done — {m}.")
                else:
                    txt = (f"Hmm, det kunne jeg ikke: {m}" if da else f"Hmm, I couldn't do that: {m}")
                return _agent_reply(uid, ustore, msg, txt)
            return await _execute_post_now(uid, ustore, current_user, pending)
        if low in _NEGATE or any(low.startswith(n) for n in _NEGATE):
            ustore.pop("agent_pending_action", None)
            txt = "Ok, jeg ændrer ikke noget. 🙂" if da else "Ok, I won't change anything. 🙂"
            return _agent_reply(uid, ustore, msg, txt)
        # Not a clear yes/no — clear stale pending and fall through to normal flow.
        ustore.pop("agent_pending_action", None)

    # 0b) Bio change → step-by-step guidance (cannot edit bio via platform API).
    if _BIO_RE.search(low):
        return _agent_reply(uid, ustore, msg, _bio_guidance(da))

    # 0c) Settings the agent can apply (niche / cadence / CTA) → confirm first.
    _set_change = None
    m_niche = _NICHE_RE.search(msg)
    m_cad = _CADENCE_RE.search(low) or _CADENCE_RE2.search(low) or _CADENCE_RE3.search(low)
    m_cta = _CTA_RE.search(msg)
    if m_niche and m_niche.group(1).strip():
        _niche_v = m_niche.group(1).strip().strip(chr(34) + chr(39) + '.')
        _set_change = ("niche", m_niche.group(1).strip().strip('"\'.'),
                       (f"sætte din niche til “{_niche_v}”" if da
                        else f"set your niche to “{_niche_v}”"))
    elif m_cad:
        _n = m_cad.group(1)
        _set_change = ("posts_per_day", _n,
                       (f"sætte din posting-frekvens til {_n} opslag/dag" if da
                        else f"set your posting cadence to {_n} posts/day"))
    elif m_cta and m_cta.group(1).strip() and "niche" not in low:
        _cta_v = m_cta.group(1).strip().strip('"\'.')
        _set_change = ("cta", _cta_v,
                       (f"gemme “{_cta_v}” som din foretrukne CTA" if da
                        else f"save “{_cta_v}” as your preferred CTA"))
    if _set_change:
        key, value, label = _set_change
        ustore["agent_pending_action"] = {"type": "apply_setting", "key": key, "value": value,
                                          "label": label, "da": da, "ts": datetime.now().isoformat()}
        _save_user_store(uid, ustore)
        if da:
            txt = f"Jeg kan {label}. Skal jeg gøre det? Svar **Ja** eller **Nej**."
        else:
            txt = f"I can {label}. Want me to do it? Reply **Yes** or **No**."
        return _agent_reply(uid, ustore, msg, txt)

    # 1) Show statistics → real numbers + navigate to stats.
    if _STATS_RE.search(low) and not _CREATE_VERB_RE.search(low):
        posts = ustore.get("scheduled_posts", [])
        pending_posts = len([p for p in posts if p.get("status") == "scheduled"])
        insights = _get_content_insights(uid)
        conns = ustore.get("connections", {})
        connected = [p for p, v in conns.items() if isinstance(v, dict) and v.get("username")]
        if insights.get("total_tracked", 0) > 0:
            avg = round(insights["avg_engagement_rate"] * 100, 1)
            if da:
                txt = (f"📊 Dine tal:\n• {insights['total_tracked']} opslag målt\n"
                       f"• Gns. engagement: {avg}%\n• Bedste platform: {insights.get('best_platform','—')}\n"
                       f"• Bedste indholdstype: {insights.get('best_content_type','—')}\n"
                       f"• Planlagte opslag: {pending_posts}")
            else:
                txt = (f"📊 Your stats:\n• {insights['total_tracked']} posts tracked\n"
                       f"• Avg engagement: {avg}%\n• Best platform: {insights.get('best_platform','—')}\n"
                       f"• Best content type: {insights.get('best_content_type','—')}\n"
                       f"• Scheduled posts: {pending_posts}")
        else:
            if da:
                txt = (f"📊 Du har endnu ingen målte opslag.\n• Forbundne platforme: {', '.join(connected) if connected else 'ingen'}\n"
                       f"• Planlagte opslag: {pending_posts}\nNår du poster, viser jeg din udvikling her.")
            else:
                txt = (f"📊 No tracked posts yet.\n• Connected platforms: {', '.join(connected) if connected else 'none'}\n"
                       f"• Scheduled posts: {pending_posts}\nOnce you post, I'll show your growth here.")
        return _agent_reply(uid, ustore, msg, txt, actions=[{"type": "navigate", "page": "stats"}])

    # 2) Schedule N posts → actually create scheduled_posts.
    if _SCHEDULE_RE.search(low) and ("opslag" in low or "post" in low):
        nums = _NUM_RE.findall(low)
        n = max(1, min(14, int(nums[0]) if nums else 1))
        platform = _detect_platform(low) or "instagram"
        topic = _extract_topic(msg)
        from datetime import timedelta as _td
        created = []
        posts = ustore.setdefault("scheduled_posts", [])
        for i in range(n):
            caption = await _gen_caption(topic, platform, "da" if da else "en")
            when = (datetime.now() + _td(days=i + 1)).replace(hour=10, minute=0, second=0, microsecond=0)
            item = {"id": (datetime.now().isoformat() + f"_{i}"), "platform": platform,
                    "content": caption, "image_url": "", "scheduled_time": when.isoformat(),
                    "status": "scheduled", "mode": "agent"}
            posts.insert(0, item)
            created.append(item)
        ustore["scheduled_posts"] = posts
        _log_agent_activity(uid, ustore, f"Scheduled {n} post(s) to {platform}", "post_content",
                            {"count": n, "platform": platform})
        if da:
            txt = (f"✅ Jeg har planlagt {n} opslag til {platform.title()}"
                   + (f" om “{topic}”" if topic else "") + " — fordelt over de næste dage kl. 10:00. "
                   "Du kan se og redigere dem i kalenderen.")
        else:
            txt = (f"✅ I've scheduled {n} post(s) to {platform.title()}"
                   + (f" about “{topic}”" if topic else "") + ", spread over the next days at 10:00. "
                   "You can view and edit them in the calendar.")
        return _agent_reply(uid, ustore, msg, txt, actions=[{"type": "navigate", "page": "posting"}])

    # 3) Post NOW to a platform → generate, then ask for confirmation.
    platform = _detect_platform(low)
    # YouTube is video-only — the agent generates a real video (text→AI image→
    # video) and uploads it. Gate behind a confirm because it costs credits +
    # takes a couple of minutes.
    if platform == "youtube" and (_POST_VERB_RE.search(low) or _CREATE_VERB_RE.search(low)):
        topic = _extract_topic(msg) or _niche_topic(ustore)
        ustore["agent_pending_action"] = {
            "type": "youtube_video", "platform": "youtube", "topic": topic,
            "da": da, "origin_msg": msg, "ts": datetime.now().isoformat(),
        }
        _save_user_store(uid, ustore)
        if da:
            txt = (f"Jeg kan lave en YouTube-video om “{topic}” (AI-billede → video) "
                   f"og uploade den. Det koster ca. 48 credits og tager et par minutter. "
                   f"Skal jeg gå i gang? Svar **Ja** eller **Nej**.")
        else:
            txt = (f"I can create a YouTube video about “{topic}” (AI image → video) "
                   f"and upload it. It costs ~48 credits and takes a couple of minutes. "
                   f"Shall I start? Reply **Yes** or **No**.")
        return _agent_reply(uid, ustore, msg, txt)

    if platform and _POST_VERB_RE.search(low):
        topic = _extract_topic(msg)
        caption = await _gen_caption(topic, platform, "da" if da else "en")
        ustore["agent_pending_action"] = {
            "type": "post_now", "platform": platform, "content": caption,
            "da": da, "origin_msg": msg, "ts": datetime.now().isoformat(),
        }
        if da:
            txt = (f"Her er et udkast til {platform.title()}:\n\n{caption}\n\n"
                   f"Skal jeg poste det nu til {platform.title()}? Svar **Ja** eller **Nej**.")
        else:
            txt = (f"Here's a draft for {platform.title()}:\n\n{caption}\n\n"
                   f"Should I post it now to {platform.title()}? Reply **Yes** or **No**.")
        return _agent_reply(uid, ustore, msg, txt)

    return None


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/api/agent/chat")
@limiter.limit("30/minute")
async def agent_chat(request: Request, req: AgentRequest, current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    billing = ustore.get("billing", store.get("billing", {}))
    plan = billing.get("plan", "free")

    # ── Voice-change intent — short-circuit before the LLM call ───────────────
    # Catch DA + EN phrases like "change voice" / "skift stemme" and reply
    # directly with the two clickable options. The frontend dispatches
    # `set_voice:<key>` actions to /api/agent/voice_settings.
    if _detect_voice_intent(req.message or ""):
        nova = VOICE_OPTIONS["nova"]; max_ = VOICE_OPTIONS["max"]
        text = (
            f"I have two voices available: {nova['name']} ({nova['description']}) "
            f"or {max_['name']} ({max_['description']}). Which do you prefer?"
        )
        # Save to history so the conversation reads naturally next time.
        _stored = _load_history(uid)
        _stored.append({"role": "user", "content": req.message})
        _stored.append({"role": "assistant", "content": text})
        _save_history(uid, _stored)
        return {
            "response":  text,
            "actions":   [
                {"type": "button", "label": f"🎙️ {nova['name']} — {nova['description']}", "action": "set_voice:nova"},
                {"type": "button", "label": f"🎙️ {max_['name']} — {max_['description']}", "action": "set_voice:max"},
            ],
            "escalate":  False,
            "voice_url": None,
        }

    # ── UgoingViral action intent-router — execute real actions, not just chat ─
    # Detects post/schedule/stats intents (and yes/no confirmation of a pending
    # post) and calls the matching UgoingViral endpoint deterministically.
    try:
        routed = await _route_ugv_action(uid, req.message or "", ustore, current_user)
    except Exception:
        routed = None
    if routed is not None:
        voice_url = None
        if (req.voice_mode or ustore.get("agent_voice_enabled", False)) and ELEVENLABS_KEY:
            voice_url = await _tts(routed.get("response", ""), ustore.get("agent_voice_id", ""))
        routed["voice_url"] = voice_url
        return routed

    agent_name = ustore.get("agent_name", "")
    ctx = _build_context(uid, req.context)
    system = _build_system(plan, ctx, agent_name)

    # ── Background task detection ─────────────────────────────────────────────
    _task_to_create  = None
    _task_limit      = TASK_LIMITS.get(plan, 0)
    _active_tasks    = [t for t in ustore.get("agent_tasks", []) if t.get("active")]
    _intent          = _detect_task_intent(req.message)
    if _intent:
        task_type, task_params = _intent
        if _task_limit == 0:
            system += (
                "\n\nAGENT NOTE: The user wants to assign you a recurring background task. "
                "They are on the Free plan (0 tasks allowed). Politely explain they need to upgrade "
                "to let you work in the background automatically. Be friendly, not pushy."
            )
        elif _task_limit != -1 and len(_active_tasks) >= _task_limit:
            system += (
                f"\n\nAGENT NOTE: User wants a background task but has reached their plan limit "
                f"({_task_limit} active tasks). Mention this naturally and suggest upgrading."
            )
        else:
            _task_to_create = (task_type, task_params)
            info = _TASK_TYPE_MAP.get(task_type, {})
            htag_note = f" for #{task_params['hashtag']}" if task_params.get("hashtag") else ""
            system += (
                f"\n\nAGENT NOTE: A new background task is being created right now "
                f"({info.get('label','task')}{htag_note}, runs {info.get('schedule','daily')}). "
                "Confirm this enthusiastically and naturally — e.g. 'Got it! I'll keep an eye on "
                "that for you \U0001f44d'. Do NOT use technical terminology or mention task IDs."
            )

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

    # Create background task if detected
    if _task_to_create:
        _create_agent_task(uid, _task_to_create[0], _task_to_create[1])

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


@router.post("/api/agent/apply_setting")
async def agent_apply_setting(req: Request, current_user: dict = Depends(get_current_user)):
    """Apply an in-app setting change requested via the agent (programmatic path
    for action buttons). Supported keys: niche, posts_per_day, cta, quiet_hours.
    Same validated helper the chat-confirm flow uses."""
    body = await req.json()
    key = str(body.get("key", "")).strip()
    value = body.get("value")
    # Honor the caller's language hint (defaults Danish — DK-first product) so the
    # returned message reads in one language.
    da = bool(body.get("da", str(body.get("lang", "da")).lower().startswith("da")))
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    ok, message = _apply_user_setting(ustore, key, value, da=da)
    if ok:
        _save_user_store(uid, ustore)
    return {"ok": ok, "message": message, "key": key}


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


@router.post("/api/agent/reset_name")
def reset_agent_name(current_user: dict = Depends(get_current_user)):
    """Clear the stored agent name so the user is re-prompted to pick one."""
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    ustore["agent_name"] = ""
    _save_user_store(uid, ustore)
    return {"ok": True, "name": ""}


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
    # Accept either a short key ("nova"/"max") or a raw ElevenLabs voice_id.
    if "voice_key" in body:
        key = str(body["voice_key"]).lower().strip()
        if key in VOICE_OPTIONS:
            ustore["agent_voice_id"] = VOICE_OPTIONS[key]["id"]
    elif "voice_id" in body:
        ustore["agent_voice_id"] = str(body["voice_id"])[:60]
    _save_user_store(current_user["id"], ustore)
    new_id  = ustore.get("agent_voice_id", "")
    new_key = _voice_key_from_id(new_id)
    return {"ok": True, "voice_id": new_id, "voice_key": new_key}


@router.get("/api/agent/voice_options")
def get_voice_options(current_user: dict = Depends(get_current_user)):
    """Return the curated voice catalog plus the user's current selection."""
    ustore = _load_user_store(current_user["id"])
    current_id  = ustore.get("agent_voice_id", VOICE_OPTIONS[DEFAULT_VOICE_KEY]["id"])
    current_key = _voice_key_from_id(current_id) or DEFAULT_VOICE_KEY
    return {
        "current_key": current_key,
        "current_id":  current_id,
        "voices": [
            {"key": k, "id": v["id"], "name": v["name"], "description": v["description"]}
            for k, v in VOICE_OPTIONS.items()
        ],
    }


@router.get("/api/agent/voice_preview/{voice_key}")
async def get_voice_preview(voice_key: str, current_user: dict = Depends(get_current_user)):
    """Generate a short TTS sample for the given voice so the Settings UI
    can play a preview without committing the change."""
    key = (voice_key or "").lower().strip()
    if key not in VOICE_OPTIONS:
        raise HTTPException(status_code=400, detail="Unknown voice")
    if not ELEVENLABS_KEY:
        raise HTTPException(status_code=503, detail="Voice preview unavailable — ElevenLabs key not set")
    sample = (
        f"Hi, I'm {VOICE_OPTIONS[key]['name']}. I'll be your AI assistant on UgoingViral."
    )
    url = await _tts(sample, VOICE_OPTIONS[key]["id"])
    if not url:
        raise HTTPException(status_code=502, detail="TTS generation failed")
    return {"voice_key": key, "voice_url": url}


# ── Agent task endpoints ───────────────────────────────────────────────────────

@router.get("/api/agent/tasks")
def get_agent_tasks(current_user: dict = Depends(get_current_user)):
    ustore = _load_user_store(current_user["id"])
    return {"tasks": ustore.get("agent_tasks", [])}


@router.patch("/api/agent/tasks/{task_id}")
async def toggle_agent_task(task_id: str, request: Request,
                            current_user: dict = Depends(get_current_user)):
    body   = await request.json()
    uid    = current_user["id"]
    ustore = _load_user_store(uid)
    tasks  = ustore.get("agent_tasks", [])
    task   = next((t for t in tasks if t["id"] == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if "active" in body:
        task["active"] = bool(body["active"])
    ustore["agent_tasks"] = tasks
    _save_user_store(uid, ustore)
    return {"ok": True, "task": task}


@router.delete("/api/agent/tasks/{task_id}")
def delete_agent_task(task_id: str, current_user: dict = Depends(get_current_user)):
    uid    = current_user["id"]
    ustore = _load_user_store(uid)
    ustore["agent_tasks"] = [t for t in ustore.get("agent_tasks", [])
                              if t["id"] != task_id]
    _save_user_store(uid, ustore)
    return {"ok": True}


@router.get("/api/agent/activity")
def get_agent_activity(current_user: dict = Depends(get_current_user)):
    from datetime import timedelta
    ustore  = _load_user_store(current_user["id"])
    log     = ustore.get("agent_activity_log", [])
    cutoff  = (datetime.now() - timedelta(days=7)).isoformat()
    recent  = [e for e in log if e.get("ts", "") >= cutoff]
    tasks   = ustore.get("agent_tasks", [])
    return {"activity": recent[-50:], "tasks": tasks}


# ── Background task runner ─────────────────────────────────────────────────────

async def run_agent_tasks():
    """Run active agent tasks once per day per schedule; log REAL results only.

    Background tasks never fabricate engagement — they report real analytics
    snapshots / scheduled-post counts, or honestly say there is nothing to report.
    """
    import asyncio as _asyncio
    await _asyncio.sleep(90)
    while True:
        try:
            from services.users import load_users
            today = datetime.now().strftime("%Y-%m-%d")
            data  = load_users()
            for u in data.get("users", []):
                uid = u.get("id")
                if not uid:
                    continue
                try:
                    ustore  = _load_user_store(uid)
                    tasks   = ustore.get("agent_tasks", [])
                    if not tasks:
                        continue
                    log     = ustore.setdefault("agent_activity_log", [])
                    changed = False
                    for task in tasks:
                        if not task.get("active"):
                            continue
                        if task.get("last_run", "")[:10] == today:
                            continue
                        if task.get("schedule") == "weekly" and datetime.now().weekday() != 0:
                            continue
                        result = await _asyncio.to_thread(_run_agent_task_real, uid, task)
                        log.append({
                            "ts":          datetime.now().isoformat(),
                            "day":         today,
                            "task_id":     task["id"],
                            "task_type":   task["type"],
                            "description": result["description"],
                            "details":     result,
                        })
                        task["last_run"]  = datetime.now().isoformat()
                        task["run_count"] = task.get("run_count", 0) + 1
                        changed = True
                    if changed:
                        ustore["agent_activity_log"] = log[-200:]
                        ustore["agent_tasks"]        = tasks
                        _save_user_store(uid, ustore)
                except Exception:
                    pass
        except Exception:
            pass
        await _asyncio.sleep(3600)
