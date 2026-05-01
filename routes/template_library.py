"""
Template Library (free for all users, managed by admin)
GET  /api/templates               — browse templates (auth)
POST /api/templates/use/{id}      — use template + increment count (auth)
GET  /api/templates/top           — top templates public (no auth)
POST /api/admin/templates         — add template (admin auth)
DELETE /api/admin/templates/{id}  — delete template (admin auth)
"""
import os, json, uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request
from routes.auth import get_current_user

router = APIRouter()

_STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "template_library.json")

_SEEDS = [
    # ── E-commerce (5) ──────────────────────────────────────────────────
    {"id": "ec-1", "title": "New Product Launch", "category": "ecommerce", "content_type": "caption",
     "caption": "🚀 Introducing [Product Name]! Finally here — tap the link in bio to shop before it sells out. Limited stock, no code needed. #newproduct #shopnow #[brand]",
     "hashtags": "#newproduct #shopnow #ecommerce #musthave #limitedstock", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ec-2", "title": "Flash Sale Urgency", "category": "ecommerce", "content_type": "caption",
     "caption": "⚡ FLASH SALE — 24 hours only! Up to [X]% off everything. Clock is ticking ⏰ Shop the link in bio. No code needed.",
     "hashtags": "#flashsale #sale #discount #limitedtime #deal", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ec-3", "title": "Customer Review Highlight", "category": "ecommerce", "content_type": "caption",
     "caption": "⭐⭐⭐⭐⭐ '[Customer quote here]' — [Customer Name]\n\nThis is why we do what we do. Thank you! Shop the link in bio.",
     "hashtags": "#customerlove #review #happycustomer #testimonial #[brand]", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ec-4", "title": "Bundle Deal Offer", "category": "ecommerce", "content_type": "caption",
     "caption": "💎 Get more, save more! Our [Bundle Name] includes everything you need at [X]% off. Perfect gift or treat yourself — link in bio!",
     "hashtags": "#bundle #deal #gifting #[brand] #sale", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ec-5", "title": "Restock Announcement", "category": "ecommerce", "content_type": "caption",
     "caption": "🙌 IT'S BACK! [Product] is restocked and ready to ship. You asked, we listened — grab yours before it's gone again. Link in bio!",
     "hashtags": "#restock #backbydemand #[brand] #shopnow #limitedstock", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},

    # ── Creator (5) ─────────────────────────────────────────────────────
    {"id": "cr-1", "title": "Behind the Scenes", "category": "creator", "content_type": "caption",
     "caption": "A peek behind the curtain 👀 This is what goes into every piece of content you see. Real work, real passion. What do you want to see next? Drop it below ⬇️",
     "hashtags": "#behindthescenes #contentcreator #dayinmylife #process #creator", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "cr-2", "title": "Story Time Hook", "category": "creator", "content_type": "hook",
     "caption": "Nobody ever told me that [relatable struggle]. But after [timeframe] of [action], here's what actually changed everything 👇 Save this post if you needed to hear it.",
     "hashtags": "#storytime #reallife #creator #motivation #contentcreator", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "cr-3", "title": "New Video / Post Teaser", "category": "creator", "content_type": "caption",
     "caption": "New video just dropped! 🎬 [What it's about in one sentence]. This one is different — watch till the end. Link in bio!",
     "hashtags": "#newvideo #contentcreator #youtuber #[niche] #newpost", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "cr-4", "title": "Collab Announcement", "category": "creator", "content_type": "caption",
     "caption": "Big news! I'm teaming up with @[partner] for something special 🎉 Stay tuned — you don't want to miss this. Comment 🔥 if you're excited!",
     "hashtags": "#collab #collaboration #partnership #[niche] #exciting", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "cr-5", "title": "Follower Milestone Thank You", "category": "creator", "content_type": "caption",
     "caption": "WE HIT [X] FOLLOWERS! 🎉 I genuinely cannot believe this. Thank you to every single one of you. To celebrate, I'm doing [giveaway/special post/Q&A] — details below!",
     "hashtags": "#milestone #thankyou #community #[niche] #blessed", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},

    # ── Agency (5) ──────────────────────────────────────────────────────
    {"id": "ag-1", "title": "Client Win Showcase", "category": "agency", "content_type": "caption",
     "caption": "Another win for our client! 🎉 [Client/brand] went from [X] to [Y] in just [timeframe] working with us. DM us 'GROW' to find out how we can do the same for your brand.",
     "hashtags": "#clientresults #digitalmarketing #agencylife #results #growth", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ag-2", "title": "Service Offering Post", "category": "agency", "content_type": "caption",
     "caption": "Struggling with [pain point]? That's exactly what we fix. Our [service] has helped [X] businesses [achieve result]. Book a free call — link in bio.",
     "hashtags": "#digitalmarketing #agency #socialmedia #marketing #freelancer", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ag-3", "title": "Industry Tip Post", "category": "agency", "content_type": "caption",
     "caption": "Most brands are making THIS mistake on [platform] 👇\n\n[Tip 1]\n[Tip 2]\n[Tip 3]\n\nSave this and share it with someone who needs it. Follow for more weekly tips!",
     "hashtags": "#marketingtips #socialmediatips #growthhacks #agency #business", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ag-4", "title": "Case Study Teaser", "category": "agency", "content_type": "caption",
     "caption": "📊 CASE STUDY: How we grew @[client] by [X]% in [timeframe]\n\nWhat we did:\n✅ [Strategy 1]\n✅ [Strategy 2]\n✅ [Strategy 3]\n\nWant the same? DM us today.",
     "hashtags": "#casestudy #marketing #results #digitalmarketing #socialmedia", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ag-5", "title": "Team Intro / Culture Post", "category": "agency", "content_type": "caption",
     "caption": "Meet the team behind [Agency Name]! 👋 We are [X] passionate people obsessed with helping brands grow online. Fun fact about us: [something unique]. Say hi in the comments!",
     "hashtags": "#teamwork #agencylife #behindthescenes #ourteam #digitalagency", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},

    # ── Restaurant (5) ──────────────────────────────────────────────────
    {"id": "rs-1", "title": "Daily Special Announcement", "category": "restaurant", "content_type": "caption",
     "caption": "Today's special: [Dish Name] 🍽️ Made fresh this morning with [key ingredient]. Available while supplies last — come in or order via link in bio!",
     "hashtags": "#dailyspecial #foodie #freshfood #[cuisine] #restaurant", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "rs-2", "title": "Weekend Brunch Promo", "category": "restaurant", "content_type": "caption",
     "caption": "Weekend brunch is BACK 🥞☀️ Join us Saturday & Sunday [time] for [dish highlights]. Book your table → link in bio. Limited spots available!",
     "hashtags": "#brunch #weekend #foodie #[location] #[cuisine]", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "rs-3", "title": "New Menu Item Drop", "category": "restaurant", "content_type": "caption",
     "caption": "NEW on our menu 🌟 Say hello to [New Dish]! [Short description of flavors]. Available starting [date]. Who's coming to try it first? Tag your food buddy ⬇️",
     "hashtags": "#newmenu #foodstagram #newdish #[cuisine] #restaurant", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "rs-4", "title": "Chef's Story Post", "category": "restaurant", "content_type": "caption",
     "caption": "Every dish tells a story 🧑‍🍳 Our [dish] is inspired by [origin/story]. Made with love and [key ingredient] sourced from [local/origin]. Come taste the difference — reservations via link in bio.",
     "hashtags": "#chefstory #localfood #[cuisine] #homemade #foodlover", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "rs-5", "title": "Happy Hour Promo", "category": "restaurant", "content_type": "caption",
     "caption": "Happy Hour starts NOW 🍹 [Time - Time], [days]. Get [offer] on [drinks/food]. Perfect way to end your day — see you here! #[location]",
     "hashtags": "#happyhour #drinks #afterwork #restaurant #[location]", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},

    # ── Fitness (5) ─────────────────────────────────────────────────────
    {"id": "ft-1", "title": "Transformation Tuesday", "category": "fitness", "content_type": "caption",
     "caption": "Transformation Tuesday 🔄 Left: [timeframe] ago. Right: today. The only thing that changed was consistency. What's your goal? Drop it below 👇",
     "hashtags": "#transformationtuesday #fitness #progress #bodygoals #nevergiveup", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ft-2", "title": "Workout of the Day", "category": "fitness", "content_type": "caption",
     "caption": "Today's workout 🔥 [Exercise 1] — [reps/sets]\n[Exercise 2] — [reps/sets]\n[Exercise 3] — [reps/sets]\n\nNo excuses! Save this for later and tag someone who needs to do this with you 💪",
     "hashtags": "#workout #wod #fitness #gymlife #fitnessmotivation", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ft-3", "title": "Morning Motivation", "category": "fitness", "content_type": "caption",
     "caption": "Your only competition is yesterday's version of you 💪 Show up today. Give everything. Trust the process. Tag someone who needs to hear this!",
     "hashtags": "#morningmotivation #fitness #mindset #neverstop #dailygrind", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ft-4", "title": "Nutrition Tip", "category": "fitness", "content_type": "caption",
     "caption": "🥗 Nutrition tip of the week: [Tip]. Most people overlook this but it makes a huge difference for [result]. Save this and try it for one week — let me know how it goes!",
     "hashtags": "#nutritiontip #healthyeating #fitness #wellness #diet", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "ft-5", "title": "Client Success Story", "category": "fitness", "content_type": "caption",
     "caption": "Meet [Client Name] 🙌 [X] weeks ago [he/she] started training with me. Today [result]. Hard work ALWAYS pays off. DM me 'READY' to start your journey!",
     "hashtags": "#clientresults #personaltrainer #fitness #transformation #fitcoach", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},

    # ── Motivation (5) ──────────────────────────────────────────────────
    {"id": "mt-1", "title": "Monday Mindset", "category": "motivation", "content_type": "caption",
     "caption": "This week, choose progress over perfection. Choose action over overthinking. Choose YOU 🙌 What's your one goal for this week? Drop it below — I'll cheer you on!",
     "hashtags": "#mondaymotivation #mindset #growth #goals #positivity", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "mt-2", "title": "Reminder Post", "category": "motivation", "content_type": "caption",
     "caption": "Friendly reminder: You don't need to have it all figured out. You just need to take the next step. That's it. Share this with someone who needs it today ❤️",
     "hashtags": "#reminder #motivation #selfcare #mentalhealth #growth", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "mt-3", "title": "Quote of the Week", "category": "motivation", "content_type": "caption",
     "caption": '"[Insert powerful quote here]"\n\n— [Author]\n\nThis one hit different. Save it for when you need it most. 🔖 Tag someone who needs this today.',
     "hashtags": "#quoteoftheday #motivation #inspiration #quotes #mindset", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "mt-4", "title": "Failure is Not Final", "category": "motivation", "content_type": "caption",
     "caption": "Every successful person has a story of failure. What separates them? They kept going anyway. Your setback is setting you up for a comeback 💥 Believe it.",
     "hashtags": "#nevergiveup #resilience #motivation #success #growth", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
    {"id": "mt-5", "title": "Weekend Reflection", "category": "motivation", "content_type": "caption",
     "caption": "Take a moment this weekend to ask yourself: What am I proud of this week? What will I do differently next week? Growth starts with reflection 🌱 Share one win from your week below!",
     "hashtags": "#weekend #reflection #growth #mindset #gratitude", "usage_count": 0, "created_at": "2026-01-01T00:00:00Z"},
]


def _load() -> list:
    if os.path.exists(_STORE):
        try:
            with open(_STORE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    _save(list(_SEEDS))
    return list(_SEEDS)


def _save(data: list):
    with open(_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _admin_check(request: Request):
    token = request.headers.get("X-Admin-Token", "")
    import os as _os
    admin_token = _os.getenv("ADMIN_JWT_SECRET", "ugv-admin-2026")
    if token != admin_token:
        raise HTTPException(status_code=403, detail="Admin access required")


@router.get("/api/templates")
def list_templates(
    category: str = "",
    q: str = "",
    current_user: dict = Depends(get_current_user),
):
    items = _load()
    if category:
        items = [t for t in items if t.get("category", "") == category]
    if q:
        ql = q.lower()
        items = [t for t in items if ql in t.get("title", "").lower() or ql in t.get("caption", "").lower()]
    items.sort(key=lambda t: t.get("usage_count", 0), reverse=True)
    return {"templates": items}


@router.get("/api/templates/top")
def top_templates():
    """Public top templates — no auth required."""
    items = _load()
    items.sort(key=lambda t: t.get("usage_count", 0), reverse=True)
    return {"templates": items[:12]}


@router.post("/api/templates/use/{template_id}")
def use_template(template_id: str, current_user: dict = Depends(get_current_user)):
    items = _load()
    for t in items:
        if t.get("id") == template_id:
            t["usage_count"] = t.get("usage_count", 0) + 1
            _save(items)
            return {"ok": True, "template": t}
    raise HTTPException(status_code=404, detail="Template not found")


@router.post("/api/admin/templates")
async def admin_add_template(request: Request):
    _admin_check(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    title = str(body.get("title", "")).strip()
    category = str(body.get("category", "other")).strip()
    content_type = str(body.get("content_type", "caption")).strip()
    caption = str(body.get("caption", "")).strip()
    hashtags = str(body.get("hashtags", "")).strip()

    if not title or not caption:
        raise HTTPException(status_code=422, detail="Title and caption are required")

    items = _load()
    new_t = {
        "id": str(uuid.uuid4())[:12],
        "title": title[:120],
        "category": category,
        "content_type": content_type,
        "caption": caption,
        "hashtags": hashtags,
        "usage_count": 0,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    items.append(new_t)
    _save(items)
    return {"ok": True, "template": new_t}


@router.delete("/api/admin/templates/{template_id}")
def admin_delete_template(template_id: str, request: Request):
    _admin_check(request)
    items = _load()
    before = len(items)
    items = [t for t in items if t.get("id") != template_id]
    if len(items) == before:
        raise HTTPException(status_code=404, detail="Template not found")
    _save(items)
    return {"ok": True}
