"""
AI Content Team — PART 1: foundation + orchestration.

A team of specialised AI agents that collaborate like a marketing agency to run
the user's content. They run in sequence, each building on the previous one's
output, coordinated by the Head of Content:

    Data Analyst → Content Strategist → Ideator → Scripter → Publishing Manager
                              (coordinated by Head of Content)

PART 1 scope (this file): each agent is a STUB that returns a clear placeholder
result. The orchestration contract (shared `context`, sequencing, persisted
activity feed, per-user goal) is real so Part 2 can drop in real logic — LLM
calls, metrics queries, scheduling — by replacing each agent's `work()` only.

Endpoints (all auth-gated, per-user store):
    GET  /api/content_team/goal     — read the user's high-level goal
    POST /api/content_team/goal     — set the goal {goal}
    GET  /api/content_team/status   — team roster + last run's activity feed
    POST /api/content_team/run      — run the 6 agents in sequence, return output
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request

from routes.auth import get_current_user
from services.store import store, save_store

router = APIRouter()

# Specialist agents run in this order. The Head of Content coordinates them and
# assembles the final plan after they finish.
AGENT_SEQUENCE = ["analyst", "strategist", "ideator", "scripter", "publisher"]

# Display metadata. Names are localised (DA/EN) so the activity feed reads
# naturally; the frontend also has matching i18n keys (ct.role.*) for the roster.
AGENTS = {
    "analyst":    {"emoji": "📊", "en": "Data Analyst",       "da": "Dataanalytiker"},
    "strategist": {"emoji": "🧭", "en": "Content Strategist", "da": "Indholdsstrateg"},
    "ideator":    {"emoji": "💡", "en": "Ideator",            "da": "Idéudvikler"},
    "scripter":   {"emoji": "✍️", "en": "Scripter",           "da": "Manuskriptforfatter"},
    "publisher":  {"emoji": "📤", "en": "Publishing Manager", "da": "Udgivelsesansvarlig"},
    "head":       {"emoji": "👑", "en": "Head of Content",    "da": "Indholdschef"},
}

GOAL_MAX = 500


def _now() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _pick(lang: str, en: str, da: str) -> str:
    return da if lang == "da" else en


def _agent_name(role: str, lang: str) -> str:
    a = AGENTS.get(role, {})
    return a.get("da" if lang == "da" else "en", role)


# ── Agents ────────────────────────────────────────────────────────────────────
class TeamAgent:
    """Base class for a Content Team agent.

    PART 1: subclasses return a clear placeholder from `work()`. PART 2 will
    replace `work()` with real logic. Every agent receives the shared `context`
    dict so later agents can build on earlier outputs — this is the orchestration
    contract Part 2 depends on, so keep it stable.
    """
    role = ""

    def __init__(self, goal: str, lang: str = "en", context: dict = None):
        self.goal = goal
        self.lang = lang
        self.context = context or {}

    @property
    def emoji(self) -> str:
        return AGENTS.get(self.role, {}).get("emoji", "•")

    def name(self) -> str:
        return _agent_name(self.role, self.lang)

    def work(self) -> dict:
        """Return {summary, output, data}. Override in Part 2 with real logic."""
        raise NotImplementedError

    def run(self) -> dict:
        """Execute the agent. One agent failing never aborts the whole run."""
        try:
            res = self.work()
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
            "stub": True,  # PART 1 marker — drop when real logic lands
            "ts": _now(),
        }


class DataAnalyst(TeamAgent):
    role = "analyst"

    def work(self):
        return {
            "summary": _pick(self.lang,
                "Analysed account metrics and audience signals",
                "Analyserede kontotal og publikumssignaler"),
            "output": _pick(self.lang,
                "Placeholder — Part 2 will pull real follower growth, top-performing "
                "posts, engagement rate and best posting times relevant to this goal.",
                "Pladsholder — Del 2 henter rigtige følgertal, bedst præsterende "
                "opslag, engagement-rate og bedste posttidspunkter for dette mål."),
            "data": {"metrics_ready": False},
        }


class ContentStrategist(TeamAgent):
    role = "strategist"

    def work(self):
        return {
            "summary": _pick(self.lang,
                "Turned the analysis into a content strategy",
                "Omsatte analysen til en indholdsstrategi"),
            "output": _pick(self.lang,
                "Placeholder — Part 2 will define content pillars, target audience, "
                "tone of voice and a posting cadence based on the Analyst's findings.",
                "Pladsholder — Del 2 fastlægger indholdssøjler, målgruppe, tone og "
                "en postkadence ud fra Analytikerens fund."),
            "data": {"pillars": []},
        }


class Ideator(TeamAgent):
    role = "ideator"

    def work(self):
        return {
            "summary": _pick(self.lang,
                "Generated content ideas from the strategy",
                "Genererede indholdsidéer ud fra strategien"),
            "output": _pick(self.lang,
                "Placeholder — Part 2 will produce concrete post ideas, hooks and "
                "angles aligned to the strategy and the platform.",
                "Pladsholder — Del 2 producerer konkrete opslagsidéer, hooks og "
                "vinkler der matcher strategien og platformen."),
            "data": {"ideas": []},
        }


class Scripter(TeamAgent):
    role = "scripter"

    def work(self):
        return {
            "summary": _pick(self.lang,
                "Drafted hooks, scripts and captions",
                "Udarbejdede hooks, manuskripter og captions"),
            "output": _pick(self.lang,
                "Placeholder — Part 2 will write full hooks, video scripts and "
                "captions with hashtags for the chosen ideas.",
                "Pladsholder — Del 2 skriver fulde hooks, video-manuskripter og "
                "captions med hashtags til de valgte idéer."),
            "data": {"scripts": []},
        }


class PublishingManager(TeamAgent):
    role = "publisher"

    def work(self):
        return {
            "summary": _pick(self.lang,
                "Planned the publishing schedule",
                "Planlagde udgivelsesplanen"),
            "output": _pick(self.lang,
                "Placeholder — Part 2 will queue the scripts into the scheduler at "
                "the best times and hand them to Auto-post.",
                "Pladsholder — Del 2 lægger manuskripterne i planlæggeren på de "
                "bedste tidspunkter og sender dem til Auto-post."),
            "data": {"scheduled": []},
        }


class HeadOfContent(TeamAgent):
    role = "head"

    def work(self):
        ran = self.context.get("ran_roles", [])
        names = ", ".join(_agent_name(r, self.lang) for r in ran)
        return {
            "summary": _pick(self.lang,
                "Coordinated the team and assembled the plan",
                "Koordinerede teamet og samlede planen"),
            "output": _pick(self.lang,
                f"Placeholder — coordinated {len(ran)} agents ({names}). Part 2 will "
                f"merge their outputs into one publish-ready plan for the goal: {self.goal}",
                f"Pladsholder — koordinerede {len(ran)} agenter ({names}). Del 2 samler "
                f"deres output til én udgivelsesklar plan for målet: {self.goal}"),
            "data": {"coordinated": ran},
        }


# role → specialist class (the Head of Content is run separately, last).
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
    """Team roster + the most recent run's activity feed (persisted per user)."""
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
    """Run the 6 agents in sequence and return their combined output.

    Specialists run in AGENT_SEQUENCE, each receiving the accumulated `context`;
    the Head of Content runs last with the full context to coordinate. The run is
    persisted as the activity feed so the UI can re-show it on next load.
    """
    body = await request.json()
    lang = "da" if (body.get("lang") == "da") else "en"
    ct = store.get("content_team", {}) or {}
    goal = (body.get("goal") or ct.get("goal") or "").strip()[:GOAL_MAX]
    if not goal:
        raise HTTPException(status_code=400, detail="No goal set — set a goal first")

    # Persist the goal if it came in with this run.
    ct["goal"] = goal
    ct.setdefault("goal_updated_at", _now())

    context = {"goal": goal, "ran_roles": [], "outputs": {}}
    steps = []
    for role in AGENT_SEQUENCE:
        step = _SPECIALISTS[role](goal, lang, context).run()
        steps.append(step)
        context["ran_roles"].append(role)
        context["outputs"][role] = step.get("output", "")

    # Head of Content coordinates last, seeing every specialist's output.
    head = HeadOfContent(goal, lang, context).run()
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
        "head": head,
        "ran_at": ct["last_run_at"],
    }
