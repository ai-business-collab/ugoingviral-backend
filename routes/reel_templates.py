"""
UgoingViral — Story/Reel Templates
GET  /api/reel-templates            → list all 20 templates
POST /api/reel-templates/{tid}/use  → generate script from template
"""
import json
from fastapi import APIRouter, Depends, Request, HTTPException
from routes.auth import get_current_user
from services.store import store, add_log

router = APIRouter()

TEMPLATES = [
  # ── E-commerce ──────────────────────────────────────────────────────────────
  {"id":"ec_drop","niche":"ecommerce","name":"Product Drop Reveal","style":"promotional",
   "duration":30,"music_mood":"Hype / Trap","description":"Build suspense around a new product drop with reveal hook",
   "hook":"Hide or blur product, then reveal with dramatic zoom","icon":"🛍️",
   "scenes":[
     {"time":"0–3s","description":"Blurred/hidden product teaser","text_overlay":"Something BIG is coming...","transition":"Zoom in"},
     {"time":"3–15s","description":"Problem the product solves — relatable moment","text_overlay":"Sound familiar? 👀","transition":"Cut"},
     {"time":"15–25s","description":"Full product reveal with key features","text_overlay":"Introducing [Product] ✨","transition":"Swipe"},
     {"time":"25–30s","description":"CTA with urgency","text_overlay":"Link in bio — limited stock!","transition":"Fade"},
   ]},
  {"id":"ec_before_after","niche":"ecommerce","name":"Before vs After","style":"entertaining",
   "duration":30,"music_mood":"Satisfying / Lo-fi","description":"Classic transformation showing product results",
   "hook":"Show the messy 'before' first — then blow them away with 'after'","icon":"✨",
   "scenes":[
     {"time":"0–3s","description":"Before state — messy, broken, or dull","text_overlay":"Before 😬","transition":"Cut"},
     {"time":"3–18s","description":"Quick montage of using the product","text_overlay":"After using [Product]...","transition":"Wipe"},
     {"time":"18–27s","description":"After result reveal — stunning transformation","text_overlay":"After ✅🔥","transition":"Zoom"},
     {"time":"27–30s","description":"CTA with guarantee","text_overlay":"Try it risk-free → link in bio","transition":"Fade"},
   ]},
  {"id":"ec_3features","niche":"ecommerce","name":"3 Features Fast","style":"educational",
   "duration":30,"music_mood":"Upbeat / Energetic","description":"Show 3 key product features in rapid-fire style",
   "hook":"Start with the most impressive feature — number it","icon":"⚡",
   "scenes":[
     {"time":"0–3s","description":"Hook: '3 reasons [product] changed my life'","text_overlay":"3 things that changed everything 👇","transition":"Cut"},
     {"time":"3–12s","description":"Feature #1 demo with reaction","text_overlay":"#1 [Feature Name]","transition":"Cut"},
     {"time":"12–21s","description":"Feature #2 demo","text_overlay":"#2 [Feature Name]","transition":"Cut"},
     {"time":"21–27s","description":"Feature #3 — save the best for last","text_overlay":"#3 [Feature Name] 🤯","transition":"Cut"},
     {"time":"27–30s","description":"CTA","text_overlay":"Get yours → link in bio","transition":"Fade"},
   ]},
  {"id":"ec_unboxing","niche":"ecommerce","name":"ASMR Unboxing","style":"entertaining",
   "duration":60,"music_mood":"ASMR / Calm lo-fi","description":"Satisfying unboxing experience with product reveal",
   "hook":"Open with the box close-up and satisfying tear sound","icon":"📦",
   "scenes":[
     {"time":"0–3s","description":"Close-up of sealed box — anticipation builds","text_overlay":"What's inside? 👀","transition":"None"},
     {"time":"3–20s","description":"Slow satisfying unboxing ASMR","text_overlay":"That packaging tho... 😍","transition":"None"},
     {"time":"20–40s","description":"First impression and feature walkthrough","text_overlay":"First thoughts...","transition":"Cut"},
     {"time":"40–55s","description":"Product in use — real reaction","text_overlay":"Does it actually work? 🤔","transition":"Cut"},
     {"time":"55–60s","description":"Verdict + CTA","text_overlay":"Rating: 10/10 🔥 Link in bio","transition":"Fade"},
   ]},

  # ── Creator ─────────────────────────────────────────────────────────────────
  {"id":"cr_dayinlife","niche":"creator","name":"Day in My Life Highlight","style":"entertaining",
   "duration":60,"music_mood":"Chill vlog / Indie","description":"Authentic lifestyle highlight reel showing your real day",
   "hook":"Start with the most aesthetic or interesting moment of your day","icon":"🌅",
   "scenes":[
     {"time":"0–3s","description":"Most cinematic moment of the day — cold open","text_overlay":"A day in my life 📍[Location]","transition":"Fade in"},
     {"time":"3–15s","description":"Morning routine highlight (3-4 clips)","text_overlay":"Morning ☀️","transition":"Jump cuts"},
     {"time":"15–35s","description":"Work/creative process BTS","text_overlay":"Work mode 💻","transition":"Cut"},
     {"time":"35–50s","description":"Fun or social moment","text_overlay":"Best part of the day 😊","transition":"Cut"},
     {"time":"50–57s","description":"Evening wind-down","text_overlay":"Evening 🌙","transition":"Cut"},
     {"time":"57–60s","description":"CTA — follow for more","text_overlay":"Follow for daily vlogs ❤️","transition":"Fade"},
   ]},
  {"id":"cr_growthtips","niche":"creator","name":"How I Grew to X Followers","style":"educational",
   "duration":60,"music_mood":"Motivational / Lo-fi beats","description":"Share the real strategy behind your growth story",
   "hook":"Lead with the surprising result number first","icon":"📈",
   "scenes":[
     {"time":"0–3s","description":"Show follower count / result first","text_overlay":"How I got [X] followers in [time]","transition":"Zoom"},
     {"time":"3–15s","description":"The starting point — humble beginning","text_overlay":"Where I started...","transition":"Cut"},
     {"time":"15–30s","description":"Strategy tip #1 — most impactful","text_overlay":"Tip #1 🔑","transition":"Cut"},
     {"time":"30–45s","description":"Strategy tip #2 — the mistake and fix","text_overlay":"The mistake I made...","transition":"Cut"},
     {"time":"45–55s","description":"Strategy tip #3 — the unlock","text_overlay":"The game changer 🚀","transition":"Cut"},
     {"time":"55–60s","description":"CTA — save and follow","text_overlay":"Save this! + Follow for part 2","transition":"Fade"},
   ]},
  {"id":"cr_quicktips","niche":"creator","name":"3 Quick Tips","style":"educational",
   "duration":30,"music_mood":"Upbeat / Snappy","description":"High-value rapid-fire tips on any topic",
   "hook":"Promise the value upfront — '3 tips that changed everything'","icon":"💡",
   "scenes":[
     {"time":"0–3s","description":"Hook — promise the value","text_overlay":"3 tips nobody tells you 🤫","transition":"Cut"},
     {"time":"3–11s","description":"Tip #1 — fastest, most actionable","text_overlay":"Tip 1: [Short title]","transition":"Cut"},
     {"time":"11–19s","description":"Tip #2 — the surprising one","text_overlay":"Tip 2: [Short title] 👀","transition":"Cut"},
     {"time":"19–27s","description":"Tip #3 — the best one, saved for last","text_overlay":"Tip 3: [Short title] 🔥","transition":"Cut"},
     {"time":"27–30s","description":"CTA — save this post","text_overlay":"Save this for later ✅","transition":"Fade"},
   ]},
  {"id":"cr_bts","niche":"creator","name":"Behind the Content","style":"entertaining",
   "duration":30,"music_mood":"Quirky / Lo-fi","description":"Show the real BTS of creating your content — funny and relatable",
   "hook":"Start with the polished final result, then cut to the chaotic reality","icon":"🎥",
   "scenes":[
     {"time":"0–3s","description":"The polished final video","text_overlay":"What you see on my page 👇","transition":"Cut"},
     {"time":"3–20s","description":"BTS chaos, re-takes, and real life interruptions","text_overlay":"What it actually takes 😅","transition":"Jump cuts"},
     {"time":"20–27s","description":"The final result vs reality side by side","text_overlay":"The process > the result","transition":"Split"},
     {"time":"27–30s","description":"CTA — follow for more BTS","text_overlay":"Follow for honest creator life 🎬","transition":"Fade"},
   ]},

  # ── Fitness ──────────────────────────────────────────────────────────────────
  {"id":"fit_transform","niche":"fitness","name":"Transformation Reveal","style":"inspirational",
   "duration":30,"music_mood":"Emotional → Epic build-up","description":"Classic physique or lifestyle transformation with emotional arc",
   "hook":"Show the most dramatic before vs after — let the visual do the talking","icon":"💪",
   "scenes":[
     {"time":"0–3s","description":"Before photo/video — honest and relatable","text_overlay":"[X] months ago...","transition":"Fade in"},
     {"time":"3–15s","description":"The journey — training montage, meals, consistency","text_overlay":"The process 🔥","transition":"Jump cuts"},
     {"time":"15–27s","description":"The reveal — current state, confident","text_overlay":"Today ✅","transition":"Dramatic zoom"},
     {"time":"27–30s","description":"CTA — inspire them to start","text_overlay":"Your turn. Link in bio 💪","transition":"Fade"},
   ]},
  {"id":"fit_morning","niche":"fitness","name":"5-Step Morning Routine","style":"educational",
   "duration":60,"music_mood":"Energetic morning vibes","description":"Show your high-performance morning routine step by step",
   "hook":"Start with your most aesthetic morning moment and the time (e.g. 5:30 AM)","icon":"🌅",
   "scenes":[
     {"time":"0–3s","description":"Wake-up shot with timestamp — aspirational","text_overlay":"5:30 AM morning routine","transition":"Fade"},
     {"time":"3–15s","description":"Step 1: Hydration / movement","text_overlay":"Step 1: [Habit]","transition":"Cut"},
     {"time":"15–27s","description":"Step 2: Exercise or breathwork","text_overlay":"Step 2: [Habit]","transition":"Cut"},
     {"time":"27–39s","description":"Step 3: Nutrition","text_overlay":"Step 3: [Habit]","transition":"Cut"},
     {"time":"39–51s","description":"Step 4+5: Mindset / planning","text_overlay":"Step 4 & 5 🧠","transition":"Cut"},
     {"time":"51–60s","description":"Result + CTA","text_overlay":"30 days changed everything. Save this ✅","transition":"Fade"},
   ]},
  {"id":"fit_challenge","niche":"fitness","name":"7-Day Challenge","style":"entertaining",
   "duration":30,"music_mood":"Hype / EDM","description":"Challenge your audience to try a specific exercise or habit",
   "hook":"Show the exercise or challenge result first — then say 'try this for 7 days'","icon":"🏋️",
   "scenes":[
     {"time":"0–3s","description":"The challenge result — your best performance","text_overlay":"POV: 7 days later... 😤","transition":"Cut"},
     {"time":"3–15s","description":"Demonstrate the challenge step by step","text_overlay":"The challenge: [Name]","transition":"Cut"},
     {"time":"15–25s","description":"Show progression — day 1 vs day 7","text_overlay":"Day 1 → Day 7","transition":"Split"},
     {"time":"25–30s","description":"CTA — comment if they're in","text_overlay":"Comment 'IN' if you're doing this ⬇️","transition":"Fade"},
   ]},
  {"id":"fit_myth","niche":"fitness","name":"Myth vs Fact","style":"educational",
   "duration":30,"music_mood":"Informative / Podcast vibes","description":"Debunk a common fitness or nutrition myth",
   "hook":"State the myth confidently — make them think you believe it — then flip it","icon":"🔬",
   "scenes":[
     {"time":"0–3s","description":"State the common myth as truth — hook with controversy","text_overlay":"Stop [doing this] ❌","transition":"Cut"},
     {"time":"3–15s","description":"The science / reality — credible explanation","text_overlay":"The truth: [Fact] ✅","transition":"Cut"},
     {"time":"15–25s","description":"What to do instead — actionable alternative","text_overlay":"Do this instead →","transition":"Cut"},
     {"time":"25–30s","description":"CTA — save and share","text_overlay":"Save this — most people get this wrong","transition":"Fade"},
   ]},

  # ── Restaurant / Food ────────────────────────────────────────────────────────
  {"id":"rest_recipe","niche":"restaurant","name":"60-Second Recipe","style":"entertaining",
   "duration":60,"music_mood":"Satisfying cooking sounds / Upbeat","description":"Fast-paced recipe video with ASMR cooking sounds and quick cuts",
   "hook":"Show the finished dish in its most beautiful plating — then say 'here's how'","icon":"🍽️",
   "scenes":[
     {"time":"0–3s","description":"Hero shot of finished dish — most beautiful angle","text_overlay":"60-second [Dish Name] 🍽️","transition":"None"},
     {"time":"3–15s","description":"Ingredient reveal — quick and visual","text_overlay":"You'll need:","transition":"Cut"},
     {"time":"15–35s","description":"Cooking process — satisfying cuts and sounds","text_overlay":"[Step names]","transition":"Cut"},
     {"time":"35–50s","description":"Plating the dish — slow and satisfying","text_overlay":"The finish 🥹","transition":"None"},
     {"time":"50–57s","description":"First bite reaction","text_overlay":"Taste test... 😍","transition":"Cut"},
     {"time":"57–60s","description":"CTA — save the recipe","text_overlay":"Save this recipe! ❤️","transition":"Fade"},
   ]},
  {"id":"rest_staffpick","niche":"restaurant","name":"Staff Picks This Week","style":"promotional",
   "duration":30,"music_mood":"Casual / Warm","description":"Highlight your best dishes this week from a staff perspective",
   "hook":"Have a real staff member speak directly to camera — authentic wins","icon":"👨‍🍳",
   "scenes":[
     {"time":"0–3s","description":"Staff member intro: 'Hi, I'm [Name], I work at [Restaurant]'","text_overlay":"Staff picks this week 👇","transition":"Cut"},
     {"time":"3–15s","description":"Dish #1 — describe it passionately, show close-up","text_overlay":"[Dish name] — [Price]","transition":"Cut"},
     {"time":"15–25s","description":"Dish #2 — the hidden gem or seasonal special","text_overlay":"This week's special ✨","transition":"Cut"},
     {"time":"25–30s","description":"CTA — make a reservation or order","text_overlay":"Reserve your table → link in bio","transition":"Fade"},
   ]},
  {"id":"rest_bts","niche":"restaurant","name":"Kitchen Behind the Scenes","style":"entertaining",
   "duration":30,"music_mood":"Energetic cooking / Hype","description":"Show the fast-paced reality of kitchen service — builds authenticity",
   "hook":"Start with the busiest moment — peak service chaos is compelling","icon":"🔥",
   "scenes":[
     {"time":"0–3s","description":"Rush hour — busy kitchen, tickets flying","text_overlay":"POV: Saturday dinner service 🔥","transition":"Cut"},
     {"time":"3–18s","description":"Chef's process — knife skills, plating, speed","text_overlay":"This is how we do it","transition":"Jump cuts"},
     {"time":"18–25s","description":"The finished plate going out","text_overlay":"Table 12, your order is ready ✅","transition":"Slow motion"},
     {"time":"25–30s","description":"CTA — come experience it","text_overlay":"Book a table → link in bio","transition":"Fade"},
   ]},
  {"id":"rest_reaction","niche":"restaurant","name":"Real Customer Reaction","style":"entertaining",
   "duration":30,"music_mood":"Feel-good / Warm","description":"Capture genuine customer reactions — the most authentic social proof",
   "hook":"The customer's face says it all — let the reaction do the talking","icon":"😋",
   "scenes":[
     {"time":"0–3s","description":"Ask customer: 'Can we film your reaction to the food?'","text_overlay":"Real customer, no script 🎬","transition":"Cut"},
     {"time":"3–18s","description":"First bite captured — unfiltered, real reaction","text_overlay":"First bite...","transition":"None"},
     {"time":"18–25s","description":"Ask: 'What do you think?' — let them describe it","text_overlay":"What they said ⬇️","transition":"Cut"},
     {"time":"25–30s","description":"Dish name + CTA","text_overlay":"[Dish name] — Come try it 🏃‍♂️","transition":"Fade"},
   ]},

  # ── Motivation ──────────────────────────────────────────────────────────────
  {"id":"mot_mindset","niche":"motivation","name":"Mindset Shift","style":"inspirational",
   "duration":30,"music_mood":"Cinematic / Emotional build","description":"One powerful perspective shift that changes how people think",
   "hook":"Open with the limiting belief — say it out loud as if you believed it too","icon":"🧠",
   "scenes":[
     {"time":"0–3s","description":"State the limiting belief — hook with relatability","text_overlay":"'I'm not ready yet...'","transition":"Fade"},
     {"time":"3–18s","description":"The reframe — tell the story or insight that changed it","text_overlay":"Then I realized...","transition":"Cut"},
     {"time":"18–27s","description":"The new belief and what it enables","text_overlay":"The truth: [Reframe]","transition":"Cut"},
     {"time":"27–30s","description":"CTA — save and share with someone who needs this","text_overlay":"Share this with someone 🙏","transition":"Fade"},
   ]},
  {"id":"mot_quote","niche":"motivation","name":"Quote + Visualization","style":"inspirational",
   "duration":15,"music_mood":"Cinematic / Piano","description":"Powerful quote with matching visuals — short and shareable",
   "hook":"The quote IS the hook — pick one that makes people stop and think","icon":"💬",
   "scenes":[
     {"time":"0–3s","description":"Open on calm, beautiful visual — nature or lifestyle","text_overlay":"[Quote — Part 1]","transition":"Fade"},
     {"time":"3–12s","description":"Continue visual journey matching the quote's emotion","text_overlay":"[Quote — Part 2 + Author]","transition":"Slow zoom"},
     {"time":"12–15s","description":"Close with your handle or brand","text_overlay":"Save this reminder 🙏 @handle","transition":"Fade out"},
   ]},
  {"id":"mot_habit","niche":"motivation","name":"One Habit That Changed Everything","style":"storytelling",
   "duration":60,"music_mood":"Build-up / Reflective","description":"Personal story arc about a single habit and its compounding impact",
   "hook":"Start with the result — what your life looks like now — then say 'one habit did this'","icon":"🔄",
   "scenes":[
     {"time":"0–3s","description":"Tease the result — your life now","text_overlay":"One habit changed everything ⬇️","transition":"Cut"},
     {"time":"3–18s","description":"The before — where you were, the struggle","text_overlay":"18 months ago...","transition":"Cut"},
     {"time":"18–35s","description":"Discovering the habit — the turning point","text_overlay":"Then I started...","transition":"Cut"},
     {"time":"35–50s","description":"The compound effect — show the progression","text_overlay":"30 days → 6 months → 1 year","transition":"Cut"},
     {"time":"50–57s","description":"The result — what it gave you","text_overlay":"Now: [Result] ✅","transition":"Cut"},
     {"time":"57–60s","description":"CTA — save and start today","text_overlay":"Save this. Start today 🔥","transition":"Fade"},
   ]},
  {"id":"mot_stopthis","niche":"motivation","name":"Stop Doing This","style":"educational",
   "duration":30,"music_mood":"Urgent / Direct beat","description":"Call out a self-sabotaging behavior and offer the fix",
   "hook":"Say 'Stop doing this' directly to camera — makes them wonder 'am I doing this?'","icon":"🛑",
   "scenes":[
     {"time":"0–3s","description":"Direct address: 'Stop [doing X]'","text_overlay":"Stop [doing this] ❌","transition":"Cut"},
     {"time":"3–15s","description":"Why it's hurting them — the hidden cost","text_overlay":"Here's why it's stopping you...","transition":"Cut"},
     {"time":"15–25s","description":"The alternative — what to do instead","text_overlay":"Do this instead ✅","transition":"Cut"},
     {"time":"25–30s","description":"CTA — save and share","text_overlay":"Save this reminder + share ❤️","transition":"Fade"},
   ]},
]

# Build lookup dict
_TMPL_MAP = {t["id"]: t for t in TEMPLATES}


@router.get("/api/reel-templates")
def list_templates(current_user: dict = Depends(get_current_user)):
    return {"ok": True, "templates": TEMPLATES, "count": len(TEMPLATES)}


@router.post("/api/reel-templates/{tid}/use")
async def generate_from_template(tid: str, req: Request,
                                  current_user: dict = Depends(get_current_user)):
    tmpl = _TMPL_MAP.get(tid)
    if not tmpl:
        raise HTTPException(404, f"Template '{tid}' not found")

    body       = await req.json()
    brand_name = str(body.get("brand_name", "")).strip()
    topic_note = str(body.get("topic_note", "")).strip()
    platform   = str(body.get("platform", "tiktok"))

    topic = brand_name or topic_note or tmpl["name"]
    if topic_note:
        topic = f"{brand_name} — {topic_note}" if brand_name else topic_note

    # Build scene descriptions into a hint string
    scene_lines = "\n".join(
        f"  • {s['time']}: {s['description']} | Overlay: \"{s['text_overlay']}\"  ({s.get('transition','')})"
        for s in tmpl["scenes"]
    )
    template_hint = (
        f"Template: {tmpl['name']}\n"
        f"Hook strategy: {tmpl['hook']}\n"
        f"Music mood: {tmpl['music_mood']}\n"
        f"Scenes:\n{scene_lines}"
    )

    # Import and call the video script logic
    from routes.video_script import _call_ai, _build_prompt
    s   = store.get("settings", {})
    raw = await _call_ai(
        _build_prompt(topic, tmpl["niche"], tmpl["duration"],
                      tmpl["style"], platform, template_hint),
        s,
    )

    s2, e2 = raw.find("{"), raw.rfind("}") + 1
    if s2 < 0 or e2 <= 0:
        raise HTTPException(500, "AI returned invalid response")
    try:
        script = json.loads(raw[s2:e2])
    except Exception as ex:
        raise HTTPException(500, f"Parse error: {ex}")

    add_log(f"📱 Template script generated: {tmpl['name']}", "success")
    return {"ok": True, "script": script, "template": tmpl,
            "meta": {"topic": topic, "duration": tmpl["duration"],
                     "style": tmpl["style"], "platform": platform}}
