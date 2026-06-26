"""
AI Content Team — orchestration + agents.

Six specialists collaborate like a marketing agency, in sequence, each building
on the previous one's output via the shared `context` dict (the stable contract):

    Data Analyst → Content Strategist → Ideator → Scripter → Publishing Manager
                              (coordinated by Head of Content)

PART 1 built the foundation (orchestration, per-user goal, activity feed, stubs).
PART 2 wires REAL logic into the first three agents:
  • Data Analyst     — reads the user's real signals (platforms, analytics,
                       recent post performance, products) → data brief.
  • Content Strategist — turns the brief + goal into a weekly content mix,
                       platforms, formats, pillars and CTAs → strategy.
  • Ideator          — uses Claude to brainstorm 15-30 ideas for the niche/goal,
                       then locks the top 7 for the week → locked ideas.
Scripter, Publishing Manager and Head of Content remain stubs until Parts 3-4.

Endpoints (auth-gated, per-user store):
    GET  /api/content_team/goal     POST /api/content_team/goal
    GET  /api/content_team/status   POST /api/content_team/run
"""
import inspect
import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store

router = APIRouter()

# Specialist agents run in this order; Head of Content coordinates last.
AGENT_SEQUENCE = ["analyst", "strategist", "ideator", "scripter", "publisher"]

AGENTS = {
    "analyst":    {"emoji": "📊", "en": "Data Analyst",       "da": "Dataanalytiker"},
    "strategist": {"emoji": "🧭", "en": "Content Strategist", "da": "Indholdsstrateg"},
    "ideator":    {"emoji": "💡", "en": "Ideator",            "da": "Idéudvikler"},
    "scripter":   {"emoji": "✍️", "en": "Scripter",           "da": "Manuskriptforfatter"},
    "publisher":  {"emoji": "📤", "en": "Publishing Manager", "da": "Udgivelsesansvarlig"},
    "head":       {"emoji": "👑", "en": "Head of Content",    "da": "Indholdschef"},
}

GOAL_MAX = 500
IDEATE_COST = 15          # credits for the Ideator's one AI brainstorm call
SCRIPT_COST = 20          # credits for the Scripter's one AI call (all scripts at once)
IDEA_TARGET = 20          # ideas requested from the model (token-efficient)
LOCKED_COUNT = 7          # winning ideas locked for the week

# Sensible default posting times per platform (local 24h) — used by the
# Publishing Manager to draft a schedule when the user has no analytics yet.
_BEST_TIMES = {
    "tiktok":    ["18:00", "12:00", "20:00"],
    "instagram": ["11:00", "19:00", "13:00"],
    "youtube":   ["15:00", "17:00"],
    "facebook":  ["09:00", "13:00"],
    "twitter":   ["08:00", "12:00", "17:00"],
    "snapchat":  ["20:00"],
    "pinterest": ["20:00", "14:00"],
}
_DEFAULT_TIMES = ["10:00", "14:00", "18:00"]

_PLATFORM_KEYS = ["tiktok", "instagram", "youtube", "facebook", "twitter", "snapchat", "pinterest"]
_PLATFORM_LABEL = {
    "tiktok": "TikTok", "instagram": "Instagram", "youtube": "YouTube",
    "facebook": "Facebook", "twitter": "X/Twitter", "snapchat": "Snapchat", "pinterest": "Pinterest",
}
_FORMATS = {
    "tiktok":    ["short-form video", "trend/hook video", "tutorial"],
    "instagram": ["Reel", "carousel", "story"],
    "youtube":   ["YouTube Short", "long-form video"],
    "facebook":  ["video post", "image post"],
    "twitter":   ["text post", "thread"],
    "snapchat":  ["story"],
    "pinterest": ["idea pin"],
}


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _pick(lang, en, da):
    return da if lang == "da" else en


def _agent_name(role, lang):
    a = AGENTS.get(role, {})
    return a.get("da" if lang == "da" else "en", role)


def _niche_from_goal(goal: str) -> str:
    """Best-effort niche from a free-text goal, e.g.
    'grow my coffee webshop on TikTok' -> 'coffee webshop'."""
    g = (goal or "").strip()
    if not g:
        return "general"
    g = re.sub(r"(?i)\s+\b(on|via|using|across|to)\s+(tiktok|instagram|ig|youtube|facebook|twitter|x|snapchat|pinterest)\b.*$", "", g).strip()
    g = re.sub(r"(?i)^(grow|scale|build|boost|get|increase|promote|launch|market|sell|run|help|drive)\s+(my|our|the|a|an)\s+", "", g).strip()
    g = re.sub(r"(?i)^(my|our)\s+", "", g).strip()
    return g or "general"


def _infer_platforms_from_goal(goal: str):
    g = (goal or "").lower()
    found = [p for p in _PLATFORM_KEYS
             if p in g
             or (p == "instagram" and re.search(r"\big\b", g))
             or (p == "twitter" and (re.search(r"\bx\b", g) or "tweet" in g))]
    return found or ["tiktok", "instagram"]


# Local-business cue words (EN + DA). Webshops are detected from product data.
_LOCAL_KEYWORDS = [
    "restaurant", "cafe", "café", "salon", "clinic", "gym", "studio", "bakery",
    "barber", "boutique", "hotel", "dealership", "shop in", "store in", "near me",
    "in town", "local business", "klinik", "frisør", "frisor", "bageri",
    "fitnesscenter", "lokal", "i byen", "butik i",
]


def _detect_user_type(signals: dict, goal: str) -> str:
    """Adapt the team to the user: webshop (has products), local_business
    (brand/venue cues in the goal), or creator/influencer (default — niche only)."""
    if signals.get("is_webshop"):
        return "webshop"
    g = (goal or "").lower()
    if any(k in g for k in _LOCAL_KEYWORDS):
        return "local_business"
    return "creator"


def _strategy_flavour(user_type: str, lang: str):
    """CTAs + content pillars tailored to the user type (bilingual)."""
    if user_type == "webshop":
        ctas = _pick(lang, ["Shop now – link in bio", "Limited-time offer", "Tap to buy"],
                           ["Køb nu – link i bio", "Tilbud i begrænset tid", "Tryk for at købe"])
        pillars = _pick(lang, ["Product highlights", "Education / how-to", "Social proof", "Trends"],
                              ["Produkt-highlights", "Undervisning / how-to", "Social proof", "Trends"])
    elif user_type == "local_business":
        ctas = _pick(lang, ["Visit us today", "Book now", "Find us nearby"],
                           ["Besøg os i dag", "Book nu", "Find os i nærheden"])
        pillars = _pick(lang, ["Behind the scenes", "Customer stories", "Local community", "Offers & events"],
                              ["Bag kulisserne", "Kundehistorier", "Lokalt fællesskab", "Tilbud & events"])
    else:  # creator / influencer — niche/style driven, no products needed
        ctas = _pick(lang, ["Follow for more", "Save this for later", "Comment below"],
                           ["Følg for mere", "Gem til senere", "Skriv en kommentar"])
        pillars = _pick(lang, ["Education / how-to", "Behind the scenes", "Storytelling", "Trends"],
                              ["Undervisning / how-to", "Bag kulisserne", "Storytelling", "Trends"])
    return ctas, pillars


# Niche/platform-optimal weekly cadence (posts/week) — best-practice guidance the
# Strategist RECOMMENDS. The user's own Auto Pilot frequency stays the source of
# truth; this is advisory and can be accepted or overridden.
_PLATFORM_CADENCE = {
    "tiktok": 10, "instagram": 7, "youtube": 3, "facebook": 5,
    "twitter": 14, "snapchat": 7, "pinterest": 7,
}


def _recommended_cadence(platforms: list) -> int:
    """Recommended posts/week = the highest-tempo connected platform's best
    practice (capped to a realistic 14/week)."""
    if not platforms:
        return 7
    rec = max(_PLATFORM_CADENCE.get(p, 5) for p in platforms)
    return max(3, min(14, rec))


def _build_slots(post_times: list, schedule_days: list, need: int) -> list:
    """Build up to `need` (date_iso, "HH:MM") slots from the user's own posting
    days (isoweekday 1=Mon…7=Sun) and time-of-day slots, starting tomorrow and
    walking forward up to 21 days. This places Content Team posts at exactly the
    times the user configured in Auto Pilot — one source of truth for cadence."""
    times = sorted({t for t in post_times if isinstance(t, str)}) or _DEFAULT_TIMES
    days = sorted({d for d in schedule_days if isinstance(d, int) and 1 <= d <= 7}) or [1, 2, 3, 4, 5, 6, 7]
    out = []
    start = datetime.utcnow().date() + timedelta(days=1)
    for offset in range(0, 21):
        d = start + timedelta(days=offset)
        if d.isoweekday() in days:
            for tm in times:
                out.append((d.isoformat(), tm))
                if len(out) >= need:
                    return out
    return out


# ── Real data collection (Data Analyst input) ───────────────────────────────
def _collect_signals(uid: str) -> dict:
    """Read the user's REAL signals from their per-user store: connected
    platforms, cached analytics, recent post performance, content volume and
    (for webshops) product names. Fast and token-free — no live API calls."""
    us = _load_user_store(uid) if uid else {}
    settings = us.get("settings", {}) or {}
    api_enabled = settings.get("api_enabled", {}) or {}
    connections = us.get("connections", {}) or {}

    platforms = sorted(
        {p for p, v in api_enabled.items() if v}
        | {p for p, v in connections.items()
           if isinstance(v, dict) and (v.get("username") or v.get("connected") or v.get("access_token"))}
    )

    perf = [p for p in (us.get("content_performance") or []) if isinstance(p, dict)]
    total_views = sum(int(p.get("views", 0) or 0) for p in perf)
    ers = [p.get("engagement_rate", 0) for p in perf if p.get("engagement_rate") is not None]
    avg_er = round(sum(ers) / len(ers), 4) if ers else 0.0
    top = sorted(perf, key=lambda x: x.get("engagement_rate", 0) or 0, reverse=True)[:3]
    top_posts = [{
        "platform": t.get("platform", ""),
        "engagement_rate": t.get("engagement_rate", 0),
        "views": int(t.get("views", 0) or 0),
        "caption": (t.get("caption") or t.get("title") or "")[:80],
    } for t in top]

    followers = 0
    for v in connections.values():
        if isinstance(v, dict):
            followers += int(v.get("followers") or v.get("follower_count") or 0)

    niche = (settings.get("niche") or us.get("niche")
             or (us.get("automation") or {}).get("niche") or "").strip()

    products = []
    for p in list(us.get("shopify_products_cache") or [])[:8] + list(us.get("manual_products") or [])[:8]:
        if isinstance(p, dict):
            nm = p.get("title") or p.get("name")
            if nm:
                products.append(str(nm)[:60])
    is_webshop = bool(settings.get("shopify_store")
                      or us.get("shopify_products_cache") or us.get("manual_products"))

    has_data = bool(perf or us.get("content_history") or us.get("scheduled_posts"))

    # Cadence = the user's OWN Auto Pilot frequency settings (one source of
    # truth). posts/week = time-slots-per-day × posting-days-per-week.
    auto = us.get("automation", {}) or {}
    post_times = [t for t in (auto.get("post_times") or []) if isinstance(t, str)]
    schedule_days = [d for d in (auto.get("schedule_days") or []) if isinstance(d, int)]
    user_per_week = (len(post_times) * len(schedule_days)) if (post_times and schedule_days) else 0
    # Product image URLs available to attach as media (free; no AI image gen exists).
    product_images = []
    for p in list(us.get("shopify_products_cache") or []) + list(us.get("manual_products") or []):
        if isinstance(p, dict):
            imgs = p.get("images") or ([p["image"]] if p.get("image") else [])
            for im in imgs:
                if isinstance(im, str) and im:
                    product_images.append(im)

    return {
        "platforms": platforms,
        "followers": followers,
        "total_views": total_views,
        "avg_engagement_rate": avg_er,
        "posts_tracked": len(perf),
        "top_posts": top_posts,
        "content_count": len(us.get("content_history") or []),
        "scheduled_count": len(us.get("scheduled_posts") or []),
        "niche": niche,
        "is_webshop": is_webshop,
        "products": products[:8],
        "product_images": product_images[:40],
        "plan": (us.get("billing", {}) or {}).get("plan", "free"),
        "has_data": has_data,
        # User's chosen cadence (source of truth) + raw slots for scheduling.
        "user_per_week": user_per_week,
        "post_times": post_times,
        "schedule_days": schedule_days,
        "automation_active": bool(auto.get("active")),
    }


# ── Credit helpers (graceful — never abort the run) ─────────────────────────
def _credits_available(cost: int) -> bool:
    try:
        from routes.billing import PLANS
        b = store.get("billing", {}) or {}
        max_cr = PLANS.get(b.get("plan", "free"), PLANS["free"])["credits"]
        return (b.get("credits", max_cr) or 0) >= cost
    except Exception:
        return False


def _charge_credits(cost: int, action: str) -> None:
    try:
        from routes.billing import PLANS
        b = store.setdefault("billing", {})
        max_cr = PLANS.get(b.get("plan", "free"), PLANS["free"])["credits"]
        cur = b.get("credits", max_cr) or 0
        b["credits"] = max(0, cur - cost)
        store["billing"] = b
        log = store.setdefault("api_usage", [])
        log.append({"action": action, "credits": cost, "ts": _now()})
        store["api_usage"] = log[-200:]
        save_store()
    except Exception:
        pass


# ── Agents ────────────────────────────────────────────────────────────────────
class TeamAgent:
    """Base class. `work()` may be sync or async; `run()` awaits if needed and
    fails soft so one agent never aborts the whole team run. Every agent shares
    the `context` dict — the contract later agents build on."""
    role = ""

    def __init__(self, goal, lang="en", context=None):
        self.goal = goal
        self.lang = lang
        self.context = context or {}

    @property
    def emoji(self):
        return AGENTS.get(self.role, {}).get("emoji", "•")

    def name(self):
        return _agent_name(self.role, self.lang)

    def work(self):
        raise NotImplementedError

    async def run(self):
        try:
            res = self.work()
            if inspect.isawaitable(res):
                res = await res
            status = "done"
        except Exception as exc:  # noqa: BLE001 — agents must fail soft
            res = {"summary": "", "output": f"error: {exc}", "data": {}}
            status = "error"
        return {
            "role": self.role,
            "emoji": self.emoji,
            "name": self.name(),
            "status": status,
            "summary": res.get("summary", ""),
            "output": res.get("output", ""),
            "data": res.get("data", {}),
            "ts": _now(),
        }


class DataAnalyst(TeamAgent):
    role = "analyst"

    def work(self):
        sig = self.context.get("signals") or {}
        lang = self.lang
        platforms = sig.get("platforms") or _infer_platforms_from_goal(self.goal)
        niche = sig.get("niche") or _niche_from_goal(self.goal)
        has_data = bool(sig.get("has_data"))

        whats_working = []
        for tp in sig.get("top_posts", [])[:3]:
            er = tp.get("engagement_rate") or 0
            if er:
                plat = _PLATFORM_LABEL.get(tp.get("platform", ""), tp.get("platform", "")) or "?"
                whats_working.append(_pick(lang,
                    f"{plat}: {round(er * 100, 1)}% engagement",
                    f"{plat}: {round(er * 100, 1)}% engagement"))

        brief = {
            "stage": "established" if has_data else "new",
            "platforms": platforms,
            "primary_platform": platforms[0],
            "niche": niche,
            "is_webshop": bool(sig.get("is_webshop")),
            "user_type": _detect_user_type(sig, self.goal),
            "products": sig.get("products", []),
            "followers": sig.get("followers", 0),
            "total_views": sig.get("total_views", 0),
            "avg_engagement_rate": sig.get("avg_engagement_rate", 0.0),
            "posts_tracked": sig.get("posts_tracked", 0),
            "top_posts": sig.get("top_posts", []),
            "whats_working": whats_working,
            "plan": sig.get("plan", "free"),
        }
        self.context["brief"] = brief

        plabel = ", ".join(_PLATFORM_LABEL.get(p, p) for p in platforms)
        if has_data:
            summary = _pick(lang, "Analysed your real account data",
                                  "Analyserede dine rigtige kontodata")
            working = "; ".join(whats_working) if whats_working else _pick(lang, "building signal", "opbygger signal")
            output = _pick(lang,
                f"{brief['posts_tracked']} posts tracked on {plabel}. "
                f"Avg engagement {round(brief['avg_engagement_rate'] * 100, 1)}%, "
                f"{brief['total_views']} total views. What's working: {working}.",
                f"{brief['posts_tracked']} opslag målt på {plabel}. "
                f"Gns. engagement {round(brief['avg_engagement_rate'] * 100, 1)}%, "
                f"{brief['total_views']} visninger i alt. Det der virker: {working}.")
        else:
            summary = _pick(lang, "No history yet — set a sensible baseline",
                                  "Ingen historik endnu — satte et fornuftigt udgangspunkt")
            output = _pick(lang,
                f"New account, no data yet. Baseline set for the {niche} niche on {plabel}, "
                f"using proven best-practices for the goal.",
                f"Ny konto uden data endnu. Udgangspunkt sat for nichen {niche} på {plabel}, "
                f"ud fra gennemprøvet best-practice for målet.")
        return {"summary": summary, "output": output, "data": brief}


class ContentStrategist(TeamAgent):
    role = "strategist"

    def work(self):
        brief = self.context.get("brief") or {}
        signals = self.context.get("signals") or {}
        lang = self.lang
        platforms = brief.get("platforms") or ["tiktok", "instagram"]
        user_type = brief.get("user_type") or ("webshop" if brief.get("is_webshop") else "creator")
        plan = brief.get("plan", "free")

        # Cadence — ONE source of truth: the user's own Auto Pilot frequency
        # (time-slots/day × posting-days/week). Fall back to a plan-based default
        # only when the user hasn't configured any. We ALSO recommend a
        # niche-optimal cadence the user can accept or override.
        user_per_week = int(signals.get("user_per_week") or 0)
        recommended = _recommended_cadence(platforms)
        if user_per_week > 0:
            per_week = user_per_week
            cadence_source = "user"
        else:
            per_week = 3 if plan == "free" else 7
            cadence_source = "default"
        per_week = max(1, min(28, per_week))
        videos = max(1, round(per_week * 0.7))
        other = max(0, per_week - videos)

        formats, seen = [], set()
        for p in platforms:
            for f in _FORMATS.get(p, ["short-form video"]):
                if f not in seen:
                    seen.add(f)
                    formats.append(f)

        # CTAs + pillars adapt to the user type (webshop / local business / creator).
        ctas, pillars = _strategy_flavour(user_type, lang)

        strategy = {
            "posts_per_week": per_week,
            "videos_per_week": videos,
            "other_posts_per_week": other,
            "recommended_per_week": recommended,
            "cadence_source": cadence_source,
            "platforms": platforms,
            "formats": formats[:6],
            "ctas": ctas,
            "pillars": pillars,
            "user_type": user_type,
            "primary_platform": brief.get("primary_platform", platforms[0]),
        }
        self.context["strategy"] = strategy

        plabel = ", ".join(_PLATFORM_LABEL.get(p, p) for p in platforms)
        if cadence_source == "user":
            cad_note = _pick(lang,
                f" (your Auto Pilot setting; niche-optimal would be ~{recommended}/week)",
                f" (din Auto Pilot-indstilling; niche-optimalt ville være ~{recommended}/uge)")
        else:
            cad_note = _pick(lang,
                f" (default — recommended for your niche: ~{recommended}/week)",
                f" (standard — anbefalet for din niche: ~{recommended}/uge)")
        summary = _pick(lang, "Set the weekly content strategy",
                              "Fastlagde den ugentlige indholdsstrategi")
        output = _pick(lang,
            f"{per_week} posts/week ({videos} videos + {other} other) across {plabel}{cad_note}. "
            f"Formats: {', '.join(strategy['formats'])}. Pillars: {', '.join(pillars)}. "
            f"CTAs: {', '.join(ctas)}.",
            f"{per_week} opslag/uge ({videos} videoer + {other} øvrige) på {plabel}{cad_note}. "
            f"Formater: {', '.join(strategy['formats'])}. Søjler: {', '.join(pillars)}. "
            f"CTA'er: {', '.join(ctas)}.")
        return {"summary": summary, "output": output, "data": strategy}


class Ideator(TeamAgent):
    role = "ideator"

    async def work(self):
        strat = self.context.get("strategy") or {}
        brief = self.context.get("brief") or {}
        lang = self.lang
        niche = brief.get("niche") or _niche_from_goal(self.goal)
        platforms = strat.get("platforms") or ["tiktok", "instagram"]
        formats = strat.get("formats") or ["short-form video"]
        pillars = strat.get("pillars") or []
        products = brief.get("products") or []

        ideas, used_ai = [], False
        # One AI brainstorm call, credit-gated. Falls back to templates on any
        # failure or if the user is out of credits — the run never breaks.
        if _credits_available(IDEATE_COST):
            try:
                ideas = await self._ai_ideas(niche, platforms, formats, pillars, products, lang)
                if ideas:
                    _charge_credits(IDEATE_COST, "content_team_ideate")
                    used_ai = True
            except Exception:
                ideas = []
        if not ideas:
            ideas = self._fallback_ideas(niche, platforms, formats, products, lang)

        # Dedupe (case-insensitive), cap, then lock the top winners for the week.
        seen, deduped = set(), []
        for i in ideas:
            k = i.strip().lower()
            if i.strip() and k not in seen:
                seen.add(k)
                deduped.append(i.strip())
        ideas = deduped[:30]
        # Lock enough ideas to FILL the user's weekly cadence (one source of
        # truth), not a fixed 7 — capped at 14/week to bound token + credit cost.
        want = int(strat.get("posts_per_week") or LOCKED_COUNT)
        lock_n = max(1, min(want, 14, len(ideas)))
        locked = ideas[:lock_n]
        self.context["ideas"] = ideas
        self.context["locked_ideas"] = locked

        summary = _pick(lang,
            f"Brainstormed {len(ideas)} ideas, locked the top {len(locked)}",
            f"Brainstormede {len(ideas)} idéer, låste de bedste {len(locked)}")
        bullet = "\n".join(f"• {x}" for x in locked)
        src = (_pick(lang, "AI-generated", "AI-genereret") if used_ai
               else _pick(lang, "starter templates (no credits / AI offline)",
                                "skabeloner (ingen credits / AI offline)"))
        output = _pick(lang,
            f"Locked {len(locked)} winning ideas for the week ({src}):\n{bullet}",
            f"Låste {len(locked)} vindende idéer for ugen ({src}):\n{bullet}")
        return {"summary": summary, "output": output,
                "data": {"ideas": ideas, "locked": locked, "ai": used_ai, "count": len(ideas)}}

    async def _ai_ideas(self, niche, platforms, formats, pillars, products, lang):
        from routes.agent import _call_openai_heavy
        plabel = ", ".join(_PLATFORM_LABEL.get(p, p) for p in platforms)
        prod_line = ""
        if products:
            prod_line = _pick(lang,
                f"\nFeature these products where relevant: {', '.join(products[:6])}.",
                f"\nFremhæv disse produkter hvor relevant: {', '.join(products[:6])}.")
        system = _pick(lang,
            "You are an expert short-form content strategist. Brainstorm concrete, "
            "scroll-stopping content ideas. Respond ONLY as a numbered list, best ideas "
            "first, one idea per line, max ~14 words each, no preamble or explanations.",
            "Du er en ekspert i short-form content. Brainstorm konkrete, fængende "
            "indholdsidéer. Svar KUN som en nummereret liste, bedste idéer først, én idé "
            "per linje, maks ~14 ord hver, ingen indledning eller forklaring.")
        user = _pick(lang,
            f"Goal: {self.goal}\nNiche: {niche}\nPlatforms: {plabel}\n"
            f"Formats: {', '.join(formats)}\nContent pillars: {', '.join(pillars)}.{prod_line}\n"
            f"Give {IDEA_TARGET} distinct ideas.",
            f"Mål: {self.goal}\nNiche: {niche}\nPlatforme: {plabel}\n"
            f"Formater: {', '.join(formats)}\nIndholdssøjler: {', '.join(pillars)}.{prod_line}\n"
            f"Giv {IDEA_TARGET} forskellige idéer.")
        text = await _call_openai_heavy(system, [{"role": "user", "content": user}],
                                        action="content_team_ideate", max_tokens=1200)
        return self._parse_ideas(text)

    @staticmethod
    def _parse_ideas(text):
        out = []
        for line in (text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            s = re.sub(r"^[\-\*•]\s*", "", s)          # bullets
            s = re.sub(r"^\d+[\.\)]\s*", "", s)         # "1." / "1)"
            s = s.strip(" -–—\t")
            if len(s) >= 4:
                out.append(s[:140])
        return out

    def _fallback_ideas(self, niche, platforms, formats, products, lang):
        p0 = _PLATFORM_LABEL.get(platforms[0], platforms[0]) if platforms else "TikTok"
        prod = products[0] if products else niche
        if lang == "da":
            base = [
                f"Hook: den største fejl folk laver med {niche}",
                f"Hurtigt {niche}-tip på 15 sekunder",
                f"Bag kulisserne af {niche}",
                f"3 myter om {niche} — afkræftet",
                f"Før/efter med {prod}",
                f"Sådan kommer du i gang med {niche} i dag",
                f"Det her ville jeg ønske jeg vidste om {niche}",
                f"Kundespørgsmål besvaret: {niche}",
                f"Trend-reaktion tilpasset {niche}",
                f"Top 5 tips til {niche}",
                f"En dag i {niche}",
                f"Hvorfor {prod} er det værd",
            ]
        else:
            base = [
                f"Hook: the #1 mistake people make with {niche}",
                f"Quick {niche} tip in 15 seconds",
                f"Behind the scenes of {niche}",
                f"3 myths about {niche} — busted",
                f"Before/after featuring {prod}",
                f"How to get started with {niche} today",
                f"What I wish I knew about {niche}",
                f"Answering a customer question about {niche}",
                f"Trending sound reaction tailored to {niche}",
                f"Top 5 tips for {niche}",
                f"A day in {niche}",
                f"Why {prod} is worth it",
            ]
        return base


class Scripter(TeamAgent):
    role = "scripter"

    async def work(self):
        locked = self.context.get("locked_ideas", []) or []
        strat = self.context.get("strategy") or {}
        brief = self.context.get("brief") or {}
        lang = self.lang
        ctas = strat.get("ctas") or _pick(lang, ["Follow for more"], ["Følg for mere"])
        niche = brief.get("niche") or _niche_from_goal(self.goal)
        if not locked:
            return {"summary": _pick(lang, "No ideas to script yet", "Ingen idéer at skrive endnu"),
                    "output": _pick(lang, "Run the Ideator first.", "Kør Idéudvikleren først."),
                    "data": {"scripts": []}}

        scripts, used_ai = [], False
        # One AI call writes ALL scripts (token-efficient), credit-gated.
        if _credits_available(SCRIPT_COST):
            try:
                scripts = await self._ai_scripts(locked, niche, ctas, lang)
                if scripts:
                    _charge_credits(SCRIPT_COST, "content_team_script")
                    used_ai = True
            except Exception:
                scripts = []
        if not scripts:
            scripts = [self._template_script(idea, ctas[0], niche, lang) for idea in locked]
        # Always align to exactly one script per locked idea.
        if len(scripts) < len(locked):
            for idea in locked[len(scripts):]:
                scripts.append(self._template_script(idea, ctas[0], niche, lang))
        scripts = scripts[:len(locked)]
        self.context["scripts"] = scripts

        sample = scripts[0]
        src = (_pick(lang, "AI-written", "AI-skrevet") if used_ai
               else _pick(lang, "templates (no credits / AI offline)",
                                "skabeloner (ingen credits / AI offline)"))
        summary = _pick(lang, f"Wrote {len(scripts)} filming-ready scripts",
                              f"Skrev {len(scripts)} optageklare manuskripter")
        output = _pick(lang,
            f"{len(scripts)} scripts ready ({src}), each as Hook → Value → CTA. "
            f"e.g. \"{sample.get('hook', '')}\" → {sample.get('cta', '')}",
            f"{len(scripts)} manuskripter klar ({src}), hver som Hook → Værdi → CTA. "
            f"f.eks. \"{sample.get('hook', '')}\" → {sample.get('cta', '')}")
        return {"summary": summary, "output": output, "data": {"scripts": scripts, "ai": used_ai}}

    async def _ai_scripts(self, locked, niche, ctas, lang):
        from routes.agent import _call_openai_heavy
        ideas_block = "\n".join(f"{i + 1}. {idea}" for i, idea in enumerate(locked))
        system = _pick(lang,
            "You are an expert short-form video scriptwriter. For EACH idea write a tight, "
            "filming-ready script using Hook → Value → CTA, punchy enough to be spoken in ~20-30s. "
            "Respond ONLY in this exact format per idea, no preamble:\n"
            "[n]\nHOOK: <1 line>\nVALUE: <1-2 lines>\nCTA: <1 line>",
            "Du er ekspert i short-form video-manuskripter. For HVER idé, skriv et stramt, "
            "optageklart manuskript med Hook → Værdi → CTA, skarpt nok til ~20-30s talt. "
            "Svar KUN i præcis dette format per idé, ingen indledning:\n"
            "[n]\nHOOK: <1 linje>\nVALUE: <1-2 linjer>\nCTA: <1 linje>")
        user = _pick(lang,
            f"Goal: {self.goal}\nNiche: {niche}\nPreferred CTAs: {', '.join(ctas[:3])}\n\nIdeas:\n{ideas_block}",
            f"Mål: {self.goal}\nNiche: {niche}\nForetrukne CTA'er: {', '.join(ctas[:3])}\n\nIdéer:\n{ideas_block}")
        text = await _call_openai_heavy(system, [{"role": "user", "content": user}],
                                        action="content_team_script", max_tokens=2000)
        return self._parse_scripts(text, locked)

    @staticmethod
    def _parse_scripts(text, locked):
        chunks = [c.strip() for c in re.split(r"\[\s*\d+\s*\]", text or "") if c.strip()]
        scripts = []
        for idx, c in enumerate(chunks):
            hook = value = cta = ""
            for line in c.splitlines():
                ls = line.strip()
                m = re.match(r"(?i)^hook\s*[:\-]\s*(.+)$", ls)
                if m:
                    hook = m.group(1).strip(); continue
                m = re.match(r"(?i)^(?:value|værdi|vaerdi)\s*[:\-]\s*(.+)$", ls)
                if m:
                    value = m.group(1).strip(); continue
                m = re.match(r"(?i)^cta\s*[:\-]\s*(.+)$", ls)
                if m:
                    cta = m.group(1).strip(); continue
            if hook or value:
                idea = locked[idx] if idx < len(locked) else ""
                scripts.append({"idea": idea, "hook": hook or idea, "value": value, "cta": cta})
        return scripts

    @staticmethod
    def _template_script(idea, cta, niche, lang):
        if lang == "da":
            return {"idea": idea, "hook": idea,
                    "value": f"Vis hurtigt 3 pointer om {niche}: problem, løsning, bevis.", "cta": cta}
        return {"idea": idea, "hook": idea,
                "value": f"Quickly show 3 beats about {niche}: problem, solution, proof.", "cta": cta}


class PublishingManager(TeamAgent):
    role = "publisher"

    def work(self):
        strat = self.context.get("strategy") or {}
        signals = self.context.get("signals") or {}
        lang = self.lang
        scripts = self.context.get("scripts") or []
        items = scripts or [{"idea": x, "hook": x} for x in self.context.get("locked_ideas", [])]
        if not items:
            return {"summary": _pick(lang, "Nothing to schedule yet", "Intet at planlægge endnu"),
                    "output": _pick(lang, "Run the Ideator and Scripter first.",
                                          "Kør Idéudvikleren og Manuskriptforfatteren først."),
                    "data": {"schedule": []}}

        # Prefer the user's ACTUALLY connected platforms; if none are connected
        # yet, fall back to the strategy's target platforms (flagged as proposed).
        connected = [p for p in (signals.get("platforms") or []) if p in _PLATFORM_KEYS]
        targets = connected or (strat.get("platforms") or ["tiktok"])

        # Schedule up to the weekly cadence, spread across the next 7 days.
        n = min(len(items), strat.get("posts_per_week", 7) or 7)
        items = items[:n]

        # Use the user's OWN Auto Pilot time slots + posting days when set
        # (one source of truth). Build dated slots over the coming week.
        user_times = signals.get("post_times") or []
        user_days = signals.get("schedule_days") or []   # 1=Mon … 7=Sun (isoweekday)
        slots = _build_slots(user_times, user_days, n) if (user_times and user_days) else None

        # Product images are the only free media source (no AI image gen exists);
        # round-robin them across posts. Video gen is opt-in (handled downstream).
        prod_imgs = [u for u in (signals.get("product_images") or []) if isinstance(u, str) and u]

        start = datetime.utcnow().date() + timedelta(days=1)
        schedule = []
        for i, it in enumerate(items):
            plat = targets[i % len(targets)]
            if slots:
                d_iso, tm = slots[i % len(slots)]
                d = datetime.fromisoformat(d_iso).date()
            else:
                day_offset = min(6, round(i * 7 / max(1, n)))
                d = start + timedelta(days=day_offset)
                times = _BEST_TIMES.get(plat, _DEFAULT_TIMES)
                tm = times[i % len(times)]
            hook = it.get("hook") or it.get("idea", "")
            # A ready-to-edit caption from the script (hook → value → CTA).
            caption = "\n\n".join(x for x in [hook, it.get("value", ""), it.get("cta", "")] if x).strip() or hook
            img = prod_imgs[i % len(prod_imgs)] if prod_imgs else ""
            schedule.append({
                "id": f"ct-{i}",
                "date": d.isoformat(),
                "day": d.strftime("%A"),
                "time": tm,
                "platform": plat,
                "platform_label": _PLATFORM_LABEL.get(plat, plat),
                "idea": it.get("idea", ""),
                "hook": hook,
                "caption": caption,
                "image_url": img,             # product image when available, else ""
                "status": "draft",            # NOT posted — user must approve
            })
        self.context["schedule"] = schedule
        self.context["schedule_connected"] = bool(connected)
        self.context["schedule_has_media"] = bool(prod_imgs)

        plats = ", ".join(sorted({s["platform_label"] for s in schedule}))
        not_connected = "" if connected else _pick(lang,
            " ⚠️ No platforms connected yet — connect accounts to publish.",
            " ⚠️ Ingen platforme forbundet endnu — forbind konti for at udgive.")
        summary = _pick(lang, "Drafted the posting schedule",
                              "Lavede udkast til udgivelsesplanen")
        output = _pick(lang,
            f"{len(schedule)} posts drafted across {plats} over the next 7 days "
            f"(draft — not posted).{not_connected}",
            f"{len(schedule)} opslag i udkast på {plats} over de næste 7 dage "
            f"(kladde — ikke postet).{not_connected}")
        return {"summary": summary, "output": output,
                "data": {"schedule": schedule, "connected": bool(connected)}}


class HeadOfContent(TeamAgent):
    role = "head"

    def work(self):
        ran = self.context.get("ran_roles", [])
        brief = self.context.get("brief") or {}
        strat = self.context.get("strategy") or {}
        locked = self.context.get("locked_ideas", []) or []
        scripts = self.context.get("scripts") or []
        schedule = self.context.get("schedule") or []
        lang = self.lang
        L = lambda en, da: _pick(lang, en, da)  # noqa: E731
        plabel = ", ".join(_PLATFORM_LABEL.get(p, p) for p in strat.get("platforms", []))

        out = []
        out.append(L(f"📋 Weekly content plan — {self.goal}",
                     f"📋 Ugentlig indholdsplan — {self.goal}"))
        out.append("")
        out.append(L("🧭 Strategy", "🧭 Strategi"))
        out.append(L(
            f"{strat.get('posts_per_week', 0)} posts/week ({strat.get('videos_per_week', 0)} video) "
            f"on {plabel or '–'}. Pillars: {', '.join(strat.get('pillars', [])) or '–'}. "
            f"CTAs: {', '.join(strat.get('ctas', [])) or '–'}.",
            f"{strat.get('posts_per_week', 0)} opslag/uge ({strat.get('videos_per_week', 0)} video) "
            f"på {plabel or '–'}. Søjler: {', '.join(strat.get('pillars', [])) or '–'}. "
            f"CTA'er: {', '.join(strat.get('ctas', [])) or '–'}."))
        out.append("")
        out.append(L(f"💡 {len(locked)} ideas + scripts", f"💡 {len(locked)} idéer + manuskripter"))
        for i, idea in enumerate(locked):
            sc = scripts[i] if i < len(scripts) else {}
            out.append(f"{i + 1}. {idea}")
            if sc:
                hook = sc.get("hook") or idea
                cta = sc.get("cta") or ""
                out.append(L(f"   Hook: {hook}" + (f" · CTA: {cta}" if cta else ""),
                             f"   Hook: {hook}" + (f" · CTA: {cta}" if cta else "")))
        out.append("")
        out.append(L("📅 Proposed schedule (draft — not posted)",
                     "📅 Foreslået plan (kladde — ikke postet)"))
        if schedule:
            for s in schedule:
                out.append(f"• {s['date']} {s['time']} · {s['platform_label']} — "
                           f"{s.get('hook') or s.get('idea')}")
        else:
            out.append(L("No schedule — connect a platform first.",
                         "Ingen plan — forbind en platform først."))
        out.append("")
        out.append(L("✅ Next steps", "✅ Næste skridt"))
        out.append(L(
            "Review and approve this plan. Publishing with your approval (human-in-the-loop) "
            "lands in Part 4.",
            "Gennemse og godkend planen. Udgivelse med din godkendelse (human-in-the-loop) "
            "kommer i Del 4."))
        text = "\n".join(out)
        self.context["weekly_brief"] = text

        return {
            "summary": _pick(lang, "Assembled your weekly content brief",
                                   "Samlede din ugentlige indholds-brief"),
            "output": text,
            "data": {
                "weekly_brief": text,
                "coordinated": ran,
                "counts": {"ideas": len(locked), "scripts": len(scripts), "scheduled": len(schedule)},
            },
        }


_SPECIALISTS = {
    "analyst": DataAnalyst,
    "strategist": ContentStrategist,
    "ideator": Ideator,
    "scripter": Scripter,
    "publisher": PublishingManager,
}

# True Auto Pilot defaults: ONE switch → fully autonomous by default.
AUTOPILOT_DEFAULTS = {
    "enabled": False,        # master switch (off until the user turns it on)
    "auto_approve": True,    # autonomous: schedule without manual review
    "interval_days": 7,      # run the team weekly
    "generate_video": False, # AI video per post is opt-in (≈250 cr each)
}


def _autopilot_cfg(ct: dict) -> dict:
    ap = dict(AUTOPILOT_DEFAULTS)
    ap.update({k: v for k, v in (ct.get("autopilot") or {}).items() if k in AUTOPILOT_DEFAULTS})
    return ap


# ── Shared pipeline + scheduling (one source of truth for the endpoint AND the
#    autopilot cycle — never duplicated) ───────────────────────────────────────
async def run_team_pipeline(uid: str, goal: str, lang: str) -> tuple:
    """Run all specialists + Head of Content in sequence over a shared context.
    Returns (steps, context). Must run inside the user's store context so the
    agents read real signals and credit-charge correctly."""
    context = {"goal": goal, "uid": uid, "ran_roles": [], "outputs": {},
               "signals": _collect_signals(uid)}
    steps = []
    for role in AGENT_SEQUENCE:
        step = await _SPECIALISTS[role](goal, lang, context).run()
        steps.append(step)
        context["ran_roles"].append(role)
        context["outputs"][role] = step.get("output", "")
    head = await HeadOfContent(goal, lang, context).run()
    steps.append(head)

    # Real credits spent this cycle (Ideator + Scripter charge only when they
    # actually used AI; templates are free).
    cost = 0
    for s in steps:
        d = s.get("data") or {}
        if s.get("role") == "ideator" and d.get("ai"):
            cost += IDEATE_COST
        if s.get("role") == "scripter" and d.get("ai"):
            cost += SCRIPT_COST
    context["cycle_cost"] = cost
    return steps, context


def _schedule_items(items: list) -> list:
    """Turn approved/auto-approved draft items into real scheduled_posts in the
    existing scheduler (reused — the same executor posts them at their time).
    Carries media (image_url/video_url) through so posts aren't text-only when a
    product image is available. Returns the created scheduled-post ids."""
    import random
    scheduled = store.setdefault("scheduled_posts", [])
    created = []
    for it in items:
        if not isinstance(it, dict):
            continue
        platform = (it.get("platform") or "instagram").strip().lower()
        date = (it.get("date") or "").strip()
        tm = (it.get("time") or "09:00").strip()
        caption = (it.get("caption") or it.get("hook") or "").strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date) or not caption:
            continue
        if not re.match(r"^\d{1,2}:\d{2}$", tm):
            tm = "09:00"
        sid = f"ct_{date}_{tm.replace(':', '')}_{random.randint(1000, 9999)}"
        scheduled.append({
            "id": sid,
            "platform": platform,
            "content": caption,
            "image_url": it.get("image_url", "") or "",
            "video_url": it.get("video_url", "") or "",
            "title": (it.get("idea") or caption)[:80],
            "scheduled_time": f"{date}T{tm}",      # fromisoformat-compatible
            "status": "scheduled",                  # picked up by the existing scheduler
            "source": "content_team",
        })
        created.append(sid)
    store["scheduled_posts"] = scheduled
    return created


async def autopilot_cycle(uid: str, lang: str = "en") -> dict:
    """One True Auto Pilot cycle: run the Content Team, then (if auto_approve)
    schedule the resulting posts automatically — otherwise leave them as a
    pending plan for the user to review. Runs in the user's store context."""
    ct = store.get("content_team", {}) or {}
    goal = (ct.get("goal") or "").strip()
    if not goal:
        return {"ok": False, "reason": "no_goal"}
    ap = _autopilot_cfg(ct)

    steps, context = await run_team_pipeline(uid, goal, lang)
    schedule = context.get("schedule", []) or []
    cost = context.get("cycle_cost", 0)

    ct["last_run_at"] = _now()
    ct["activity"] = steps
    ct["scripts"] = context.get("scripts", [])
    ct["weekly_brief"] = context.get("weekly_brief", "")
    ct["last_autorun_at"] = _now()
    ct["last_cycle_cost"] = cost
    ct["last_cycle_drafted"] = len(schedule)

    scheduled_ids = []
    if ap["auto_approve"] and schedule:
        scheduled_ids = _schedule_items(schedule)
        ct["schedule"] = []                  # consumed — they're real scheduled posts now
        ct["approved_total"] = (ct.get("approved_total", 0) or 0) + len(scheduled_ids)
        ct["last_approved_at"] = _now()
    else:
        ct["schedule"] = schedule            # pending review
    ct["last_cycle_scheduled"] = len(scheduled_ids)
    store["content_team"] = ct
    save_store()

    try:
        from routes.notifications import push_notification
        if ap["auto_approve"]:
            push_notification(uid, "autopilot_content", "Auto Pilot ran your Content Team",
                              f"{len(scheduled_ids)} posts scheduled automatically (~{cost} credits).")
        else:
            push_notification(uid, "autopilot_content", "Content Team plan ready to review",
                              f"{len(schedule)} posts drafted — review & approve in Content Team.")
    except Exception:
        pass
    return {"ok": True, "auto_approve": ap["auto_approve"],
            "scheduled": len(scheduled_ids), "drafted": len(schedule), "cost": cost}


# ── Endpoints ───────────────────────────────────────────────────────────────
@router.get("/api/content_team/goal")
def get_goal(current_user: dict = Depends(get_current_user)):
    ct = store.get("content_team", {}) or {}
    return {"goal": ct.get("goal", ""), "updated_at": ct.get("goal_updated_at", "")}


@router.post("/api/content_team/goal")
async def set_goal(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    goal = (body.get("goal") or "").strip()[:GOAL_MAX]
    ct = store.get("content_team", {}) or {}
    ct["goal"] = goal
    ct["goal_updated_at"] = _now()
    store["content_team"] = ct
    save_store()
    return {"ok": True, "goal": goal}


@router.get("/api/content_team/status")
def get_status(current_user: dict = Depends(get_current_user)):
    ct = store.get("content_team", {}) or {}
    roster = [
        {"role": r, "emoji": AGENTS[r]["emoji"], "name": AGENTS[r]["en"]}
        for r in AGENT_SEQUENCE + ["head"]
    ]
    return {
        "goal": ct.get("goal", ""),
        "last_run_at": ct.get("last_run_at", ""),
        "activity": ct.get("activity", []),
        "schedule": ct.get("schedule", []),
        "weekly_brief": ct.get("weekly_brief", ""),
        "roster": roster,
        "autopilot": _autopilot_cfg(ct),
        "last_autorun_at": ct.get("last_autorun_at", ""),
        "last_cycle_cost": ct.get("last_cycle_cost", 0),
        "last_cycle_scheduled": ct.get("last_cycle_scheduled", 0),
        # Per-cycle credit cost (Ideator 15 + Scripter 20 when AI is used).
        "cycle_cost_estimate": IDEATE_COST + SCRIPT_COST,
        "approved_total": ct.get("approved_total", 0),
    }


@router.get("/api/content_team/autopilot")
def get_autopilot(current_user: dict = Depends(get_current_user)):
    ct = store.get("content_team", {}) or {}
    return {
        "autopilot": _autopilot_cfg(ct),
        "goal": ct.get("goal", ""),
        "last_autorun_at": ct.get("last_autorun_at", ""),
        "cycle_cost_estimate": IDEATE_COST + SCRIPT_COST,
    }


@router.post("/api/content_team/autopilot")
async def set_autopilot(request: Request, current_user: dict = Depends(get_current_user)):
    """Configure True Auto Pilot. ONE switch (`enabled`) → the scheduler runs the
    Content Team on `interval_days` and (if `auto_approve`) schedules the posts
    automatically. Requires a goal before it can be enabled."""
    body = await request.json()
    ct = store.get("content_team", {}) or {}
    ap = _autopilot_cfg(ct)
    if "enabled" in body:
        enabled = bool(body["enabled"])
        if enabled and not (ct.get("goal") or "").strip():
            raise HTTPException(status_code=400, detail="Set a goal first, then enable Auto Pilot.")
        ap["enabled"] = enabled
    if "auto_approve" in body:
        ap["auto_approve"] = bool(body["auto_approve"])
    if "interval_days" in body:
        try:
            ap["interval_days"] = max(1, min(30, int(body["interval_days"])))
        except Exception:
            pass
    if "generate_video" in body:
        ap["generate_video"] = bool(body["generate_video"])
    ct["autopilot"] = ap
    store["content_team"] = ct
    save_store()
    return {"ok": True, "autopilot": ap}


@router.post("/api/content_team/approve")
async def approve_schedule(request: Request, current_user: dict = Depends(get_current_user)):
    """Human-in-the-loop: turn the user-APPROVED draft items into real
    scheduled_posts in the existing scheduler (reused — not duplicated). Only the
    items sent here are scheduled; rejected items are simply never sent. Nothing
    is published until each post's scheduled time, exactly like any other
    scheduled post."""
    body = await request.json()
    items = body.get("items") or []
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="No items to approve")

    created = _schedule_items(items)   # shared with the autopilot cycle

    # Record approval and drop the approved drafts from the pending plan.
    ct = store.get("content_team", {}) or {}
    approved_ids = {it.get("id") for it in items if isinstance(it, dict) and it.get("id")}
    if approved_ids:
        ct["schedule"] = [s for s in (ct.get("schedule") or []) if s.get("id") not in approved_ids]
    ct["last_approved_at"] = _now()
    ct["approved_total"] = (ct.get("approved_total", 0) or 0) + len(created)
    store["content_team"] = ct
    save_store()
    return {"ok": True, "scheduled": len(created), "ids": created,
            "remaining": len(ct.get("schedule", []))}


@router.post("/api/content_team/run")
async def run_team(request: Request, current_user: dict = Depends(get_current_user)):
    """Run the team in sequence and return their combined output. Specialists
    each see the accumulated `context`; the Head of Content coordinates last.
    The run is persisted as the per-user activity feed."""
    body = await request.json()
    lang = "da" if (body.get("lang") == "da") else "en"
    uid = current_user["id"]
    ct = store.get("content_team", {}) or {}
    goal = (body.get("goal") or ct.get("goal") or "").strip()[:GOAL_MAX]
    if not goal:
        raise HTTPException(status_code=400, detail="No goal set — set a goal first")
    ct["goal"] = goal
    ct.setdefault("goal_updated_at", _now())

    # Shared pipeline — identical code path the autopilot cycle uses.
    steps, context = await run_team_pipeline(uid, goal, lang)
    head = steps[-1]

    ct["last_run_at"] = _now()
    ct["activity"] = steps
    # Persist the draft artifacts so the page can re-show them and approve later.
    ct["schedule"] = context.get("schedule", [])
    ct["scripts"] = context.get("scripts", [])
    ct["weekly_brief"] = context.get("weekly_brief", "")
    store["content_team"] = ct
    save_store()

    return {
        "ok": True,
        "goal": goal,
        "lang": lang,
        "steps": steps,
        "brief": context.get("brief", {}),
        "strategy": context.get("strategy", {}),
        "locked_ideas": context.get("locked_ideas", []),
        "scripts": context.get("scripts", []),
        "schedule": context.get("schedule", []),
        "weekly_brief": context.get("weekly_brief", ""),
        "head": head,
        "ran_at": ct["last_run_at"],
    }
