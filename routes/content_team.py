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
from datetime import datetime

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
IDEA_TARGET = 20          # ideas requested from the model (token-efficient)
LOCKED_COUNT = 7          # winning ideas locked for the week

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
        "plan": (us.get("billing", {}) or {}).get("plan", "free"),
        "has_data": has_data,
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
        lang = self.lang
        platforms = brief.get("platforms") or ["tiktok", "instagram"]
        is_shop = bool(brief.get("is_webshop"))
        plan = brief.get("plan", "free")

        # Cadence scales with plan: Free starts light, paid plans post more.
        per_week = 3 if plan == "free" else 7
        videos = max(1, round(per_week * 0.7))
        other = max(0, per_week - videos)

        formats, seen = [], set()
        for p in platforms:
            for f in _FORMATS.get(p, ["short-form video"]):
                if f not in seen:
                    seen.add(f)
                    formats.append(f)

        ctas = _pick(lang,
            (["Shop now – link in bio", "Limited-time offer", "Tap to buy"] if is_shop
             else ["Follow for more", "Save this for later", "Comment below"]),
            (["Køb nu – link i bio", "Tilbud i begrænset tid", "Tryk for at købe"] if is_shop
             else ["Følg for mere", "Gem til senere", "Skriv en kommentar"]))

        pillars = _pick(lang,
            ["Education / how-to", "Behind the scenes", "Social proof", "Trends"],
            ["Undervisning / how-to", "Bag kulisserne", "Social proof", "Trends"])
        if is_shop:
            pillars = pillars[:3] + _pick(lang, ["Product highlights"], ["Produkt-highlights"])

        strategy = {
            "posts_per_week": per_week,
            "videos_per_week": videos,
            "other_posts_per_week": other,
            "platforms": platforms,
            "formats": formats[:6],
            "ctas": ctas,
            "pillars": pillars,
            "primary_platform": brief.get("primary_platform", platforms[0]),
        }
        self.context["strategy"] = strategy

        plabel = ", ".join(_PLATFORM_LABEL.get(p, p) for p in platforms)
        summary = _pick(lang, "Set the weekly content strategy",
                              "Fastlagde den ugentlige indholdsstrategi")
        output = _pick(lang,
            f"{per_week} posts/week ({videos} videos + {other} other) across {plabel}. "
            f"Formats: {', '.join(strategy['formats'])}. Pillars: {', '.join(pillars)}. "
            f"CTAs: {', '.join(ctas)}.",
            f"{per_week} opslag/uge ({videos} videoer + {other} øvrige) på {plabel}. "
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
        locked = ideas[:LOCKED_COUNT]
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
        from routes.agent import _call_claude_heavy
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
        text = await _call_claude_heavy(system, [{"role": "user", "content": user}],
                                        action="content_team_ideate")
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

    def work(self):
        # PART 3 will write full hooks/scripts/captions from the locked ideas.
        locked = self.context.get("locked_ideas", [])
        return {
            "summary": _pick(self.lang, "Ready to script the locked ideas",
                                        "Klar til at skrive de låste idéer"),
            "output": _pick(self.lang,
                f"Placeholder — Part 3 will turn the {len(locked)} locked ideas into full "
                "hooks, video scripts and captions with hashtags.",
                f"Pladsholder — Del 3 omsætter de {len(locked)} låste idéer til fulde hooks, "
                "video-manuskripter og captions med hashtags."),
            "data": {"scripts": []},
        }


class PublishingManager(TeamAgent):
    role = "publisher"

    def work(self):
        # PART 4 will queue scripts into the scheduler at the best times.
        strat = self.context.get("strategy") or {}
        return {
            "summary": _pick(self.lang, "Ready to schedule the week",
                                        "Klar til at planlægge ugen"),
            "output": _pick(self.lang,
                f"Placeholder — Part 4 will schedule {strat.get('posts_per_week', 0)} posts/week "
                "into the scheduler at the best times and hand them to Auto-post.",
                f"Pladsholder — Del 4 planlægger {strat.get('posts_per_week', 0)} opslag/uge i "
                "planlæggeren på de bedste tidspunkter og sender dem til Auto-post."),
            "data": {"scheduled": []},
        }


class HeadOfContent(TeamAgent):
    role = "head"

    def work(self):
        ran = self.context.get("ran_roles", [])
        brief = self.context.get("brief") or {}
        strat = self.context.get("strategy") or {}
        locked = self.context.get("locked_ideas", [])
        names = ", ".join(_agent_name(r, self.lang) for r in ran)
        return {
            "summary": _pick(self.lang, "Coordinated the team and assembled the plan",
                                        "Koordinerede teamet og samlede planen"),
            "output": _pick(self.lang,
                f"Coordinated {len(ran)} agents ({names}). This week: {strat.get('posts_per_week', 0)} "
                f"posts across {len(strat.get('platforms', []))} platform(s) in the "
                f"{brief.get('niche', '?')} niche, with {len(locked)} ideas locked. "
                "Parts 3-4 will script and schedule them.",
                f"Koordinerede {len(ran)} agenter ({names}). Denne uge: {strat.get('posts_per_week', 0)} "
                f"opslag på {len(strat.get('platforms', []))} platform(e) i nichen "
                f"{brief.get('niche', '?')}, med {len(locked)} idéer låst. "
                "Del 3-4 skriver og planlægger dem."),
            "data": {"coordinated": ran, "locked_ideas": locked},
        }


_SPECIALISTS = {
    "analyst": DataAnalyst,
    "strategist": ContentStrategist,
    "ideator": Ideator,
    "scripter": Scripter,
    "publisher": PublishingManager,
}


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
        "roster": roster,
    }


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

    # Real signals gathered once, shared with the whole team via context.
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

    ct["last_run_at"] = _now()
    ct["activity"] = steps
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
        "head": head,
        "ran_at": ct["last_run_at"],
    }
