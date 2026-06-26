"""
UgoingViral — FastAPI Backend
Koden er splittet i moduler under routes/ og services/
"""
import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from services.store import set_user_context, reset_user_context
from services.security import (
    limiter,
    security_headers_middleware,
    body_size_middleware,
    rate_limit_handler,
    InputValidationMiddleware,
)
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from routes import settings, products, content, posts, automation, playwright, instagram, tiktok, youtube, twitter, scheduler, email, auth, billing, admin, onboarding, stats, agent, subaccounts, studio, uploads, content_engine, autopilot, telegram, growth, nexora_core, nexora_events, qa_agent, affiliate, template_library, analytics, notifications, brand_kit, competitor, viral_score, caption_improver, hashtags, csv_import, video_script, reel_templates, audit, workspaces, ig_growth, webhooks, content_library, content_team

app = FastAPI(title="UgoingViral API v4")

# Rate limiter (slowapi) — default 60/min per user (or per IP if unauthenticated).
# Specific endpoints declare tighter limits via @limiter.limit(...) decorators.
app.state.limiter = limiter
# Friendly 429 ("Too many requests — please wait a moment") for any breach.
app.add_exception_handler(RateLimitExceeded, rate_limit_handler)
app.add_middleware(SlowAPIMiddleware)

# Security: 10 MB body cap + standard hardening headers on every response.
app.middleware("http")(body_size_middleware)
app.middleware("http")(security_headers_middleware)

# CORS — restrict to the production domain only.
ALLOWED_ORIGINS = [
    "https://ugoingviral.com",
    "https://www.ugoingviral.com",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Reject SQLi / XSS payloads globally before they reach any route handler.
# Added last so it is the outermost middleware (buffers/replays the body once).
app.add_middleware(InputValidationMiddleware)

@app.middleware("http")
async def user_store_middleware(request: Request, call_next):
    from routes.auth import SECRET_KEY, ALGORITHM
    from jose import JWTError, jwt as _jwt
    from services.store import _load_user_store
    auth_header = request.headers.get("Authorization", "")
    tokens = None
    if auth_header.startswith("Bearer "):
        try:
            payload = _jwt.decode(auth_header[7:], SECRET_KEY, algorithms=[ALGORITHM])
            user_id = payload.get("user_id")
            if user_id:
                # Support sub-account context via header
                sub_id = request.headers.get("X-Sub-Account-Id", "").strip()
                effective_id = user_id
                if sub_id:
                    ustore = _load_user_store(user_id)
                    subs = ustore.get("sub_accounts", [])
                    if any(s["id"] == sub_id for s in subs):
                        effective_id = f"{user_id}_sub_{sub_id}"
                tokens = set_user_context(effective_id)
        except Exception:
            pass
    response = await call_next(request)
    if tokens:
        reset_user_context(tokens)
    return response


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Time every request and track active identities for /api/health/detailed."""
    import time as _t
    start = _t.perf_counter()
    response = await call_next(request)
    try:
        from services.metrics import record_request
        from services.security import _rate_key
        record_request((_t.perf_counter() - start) * 1000.0, _rate_key(request))
    except Exception:
        pass
    return response

os.makedirs("uploads", exist_ok=True)
os.makedirs("uploads/user_media", exist_ok=True)
os.makedirs("uploads/creators", exist_ok=True)
os.makedirs("uploads/voice", exist_ok=True)
os.makedirs("uploads/final", exist_ok=True)
os.makedirs("uploads/studio", exist_ok=True)
os.makedirs("user_content", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
app.mount("/user_content", StaticFiles(directory="user_content"), name="user_content")

FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "index.html")
ADMIN_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "admin.html")

@app.get("/login")
def login_page():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/?login=1", status_code=302)

@app.get("/admin")
def admin_page():
    if os.path.exists(ADMIN_HTML):
        return FileResponse(ADMIN_HTML, media_type="text/html")
    return {"msg": "Admin panel ikke fundet"}

@app.get("/delete-data")
def delete_data_page():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "delete-data.html")
    return FileResponse(p, media_type="text/html")

@app.get("/reset-password")
def reset_password_page():
    # File is named reset-pw.html (the repo blocks '*password*' filenames); the
    # public URL stays /reset-password.
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "reset-pw.html")
    return FileResponse(p, media_type="text/html")

@app.get("/app")
def serve_frontend(request: Request):
    # Tjek om bruger er logget ind
    token = request.cookies.get("ugv_token") or request.headers.get("X-Token")
    # Simpel check — send til login hvis ikke logget ind
    # Frontend håndterer token via localStorage
    if os.path.exists(FRONTEND):
        return FileResponse(FRONTEND, media_type="text/html")
    return {"msg": "Frontend ikke fundet"}


@app.get("/robots.txt")
def robots_txt():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "robots.txt"), media_type="text/plain")


@app.get("/sitemap.xml")
def sitemap_xml():
    from fastapi.responses import PlainTextResponse
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://ugoingviral.com/</loc><priority>1.0</priority></url>
  <url><loc>https://ugoingviral.com/app</loc><priority>0.8</priority></url>
</urlset>"""
    return PlainTextResponse(xml, media_type="application/xml")


@app.get("/tiktok0pPHr8lF9An0dqJulvuzcW8LH93JdBeb.txt")
def tiktok_verify():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "tiktok0pPHr8lF9An0dqJulvuzcW8LH93JdBeb.txt"), media_type="text/plain")


@app.get("/tiktokFP3k9ZuaAI7YQzz7LN1nmGdk1XKrZsRF.txt")
def tiktok_verify2():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "tiktokFP3k9ZuaAI7YQzz7LN1nmGdk1XKrZsRF.txt"), media_type="text/plain")


@app.get("/tiktokSw68ZsEm2ws9Y0yr73osmK8HUtYmyHIN.txt")
def tiktok_verify3():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "tiktokSw68ZsEm2ws9Y0yr73osmK8HUtYmyHIN.txt"), media_type="text/plain")


@app.get("/tiktok50KIALdvtg3pFgadwqJ17IpICFOwbkpT.txt")
def tiktok_verify4():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "tiktok50KIALdvtg3pFgadwqJ17IpICFOwbkpT.txt"), media_type="text/plain")

@app.get("/google165f5389ae0e6661.html")
def google_site_verification():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "google165f5389ae0e6661.html"), media_type="text/html")


@app.get("/tiktok5DeyUFarAWVKpYgiI4AY3N60qCV7OMUL.txt")
def tiktok_verification():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("tiktok-developers-site-verification=5DeyUFarAWVKpYgiI4AY3N60qCV7OMUL")


@app.get("/tiktokUJvH5NFKcwM74RMxCIlNWbMBVKicbMp1.txt")
def tiktok_verification_sandbox():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("tiktok-developers-site-verification=UJvH5NFKcwM74RMxCIlNWbMBVKicbMp1")

@app.get("/privacy")
def privacy_policy():
    from fastapi.responses import HTMLResponse
    import os
    path = os.path.join(os.path.dirname(__file__), "frontend", "privacy.html")
    with open(path, "r") as f:
        html = f.read()
    return HTMLResponse(content=html)

@app.get("/terms")
def terms():
    from fastapi.responses import HTMLResponse
    import os
    path = os.path.join(os.path.dirname(__file__), "frontend", "terms.html")
    with open(path, "r") as f:
        html = f.read()
    return HTMLResponse(content=html)

@app.get("/data-deletion")
def data_deletion():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Data Deletion - UgoingViral</title></head>
<body style="font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px">
<h1>Data Deletion Request</h1>
<p>To request deletion of your data, please email us at: support@ugoingviral.com</p>
<p>We will process your request within 30 days.</p>
</body></html>"""
    return HTMLResponse(content=html)

@app.get("/")
@app.get("/landing")
def landing_page():
    from fastapi.responses import FileResponse
    import os
    landing = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "landing.html")
    if os.path.exists(landing):
        return FileResponse(landing, media_type="text/html")
    return {"msg": "Landing page ikke fundet"}

@app.get("/health")
def health():
    from datetime import datetime
    return {"status": "healthy", "timestamp": datetime.now().isoformat(), "version": "4.1"}


@app.get("/api/health/detailed")
def health_detailed(_owner=Depends(admin.require_owner)):
    """Detailed ops/load-test view: active users, DB sizes, memory, response
    times, and video-queue status. Owner-only (leaks infra internals)."""
    from datetime import datetime, timezone
    from services.metrics import response_time_stats, active_stats, memory_usage
    from services.cache import cache_stats
    from routes.studio import queue_stats

    base = os.path.dirname(os.path.abspath(__file__))

    # Database / JSON store file sizes.
    db_files = [
        "users.json", "data.json", "admin_users.json", "security_log.json",
        "nexora_events.json", "customer_assignments.json", "stripe_products.json",
        "template_library.json",
    ]
    sizes = {}
    total_bytes = 0
    for fn in db_files:
        p = os.path.join(base, fn)
        if os.path.exists(p):
            sz = os.path.getsize(p)
            sizes[fn] = round(sz / 1024 / 1024, 3)
            total_bytes += sz

    # Per-user store directory rollup.
    ud_dir = os.path.join(base, "user_data")
    ud_bytes = ud_count = 0
    if os.path.isdir(ud_dir):
        for f in os.listdir(ud_dir):
            fp = os.path.join(ud_dir, f)
            if os.path.isfile(fp):
                ud_bytes += os.path.getsize(fp)
                ud_count += 1

    # Registered users + how many logged in within the last 24h.
    total_users = active_24h = None
    try:
        from services.users import load_users
        users = load_users().get("users", [])
        total_users = len(users)
        now = datetime.now(timezone.utc)
        active_24h = 0
        for u in users:
            ll = u.get("last_login")
            if not ll:
                continue
            try:
                dt = datetime.fromisoformat(str(ll).replace("Z", ""))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if (now - dt).total_seconds() <= 86400:
                    active_24h += 1
            except Exception:
                pass
    except Exception:
        pass

    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "users": {
            "registered_total": total_users,
            "logged_in_last_24h": active_24h,
            **active_stats(),
        },
        "database_files_mb": sizes,
        "database_total_mb": round(total_bytes / 1024 / 1024, 3),
        "user_data_dir": {
            "files": ud_count,
            "total_mb": round(ud_bytes / 1024 / 1024, 3),
        },
        "memory": memory_usage(),
        "response_times_last_100": response_time_stats(),
        "video_queue": queue_stats(),
        "response_cache": cache_stats(),
    }

# Inkluder alle routes
app.include_router(auth.router)
app.include_router(settings.router)
app.include_router(products.router)
app.include_router(content.router)
app.include_router(posts.router)
app.include_router(automation.router)
app.include_router(playwright.router)
app.include_router(instagram.router)
app.include_router(tiktok.router)
app.include_router(youtube.router)
app.include_router(twitter.router)
app.include_router(scheduler.router)
app.include_router(email.router)
app.include_router(billing.router)
app.include_router(admin.router)
app.include_router(onboarding.router)
app.include_router(stats.router)
app.include_router(agent.router)
app.include_router(subaccounts.router)
app.include_router(studio.router)
app.include_router(uploads.router)
app.include_router(content_library.router)
app.include_router(content_team.router)
# Leads CRM disabled — UgoingViral does not use CRM. Kept for the Reachr product.
# To re-enable: add `leads` back to the `from routes import ...` line above and
# uncomment the line below.
# app.include_router(leads.router)
app.include_router(content_engine.router)
app.include_router(autopilot.router)
app.include_router(telegram.router)
app.include_router(growth.router)
app.include_router(nexora_core.router)
app.include_router(nexora_events.router)
app.include_router(qa_agent.router)
app.include_router(affiliate.router)
app.include_router(template_library.router)
app.include_router(analytics.router)
app.include_router(notifications.router)
app.include_router(brand_kit.router)
app.include_router(competitor.router)
app.include_router(viral_score.router)
app.include_router(caption_improver.router)
app.include_router(hashtags.router)
app.include_router(csv_import.router)
app.include_router(video_script.router)
app.include_router(reel_templates.router)
app.include_router(audit.router)
app.include_router(ig_growth.router)
app.include_router(workspaces.router)
app.include_router(webhooks.router)


@app.get("/affiliate")
async def serve_affiliate():
    from fastapi.responses import FileResponse
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "affiliate.html")
    return FileResponse(p)


# ── Multi-worker leader election ─────────────────────────────────────────────
# With >1 uvicorn worker the startup hook runs in EVERY worker process. The
# background loops below (scheduler, autopilot, telegram reports, auto-DM,
# analytics sync, cleanup) MUST run on exactly one worker — otherwise scheduled
# posts get published N times, credit/billing checks run N times, etc. We elect
# a single leader with a non-blocking exclusive flock on a lockfile: the first
# worker to grab it runs the loops; the others become stateless request servers.
# The lock is held for the process lifetime (the fh is kept alive intentionally)
# and is released automatically by the OS if that worker dies, so another worker
# can take over on the next start.
import logging as _logging
logger = _logging.getLogger("ugoingviral")

# Lock path is namespaced by this deployment's directory so a second copy of the
# app on the same host (e.g. a load-test clone) can never steal the production
# scheduler lock — each deployment elects its own single leader.
import hashlib as _hashlib
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_LEADER_LOCK_PATH = os.path.join(
    "/tmp", f"ugoingviral_scheduler_{_hashlib.md5(_APP_DIR.encode()).hexdigest()[:8]}.lock"
)
_leader_lock_fh = None


def _try_become_scheduler_leader() -> bool:
    global _leader_lock_fh
    import fcntl
    try:
        fh = open(_LEADER_LOCK_PATH, "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _leader_lock_fh = fh  # keep ref alive → lock held for process lifetime
        return True
    except Exception:
        return False


@app.on_event("startup")
async def startup_event():
    from services.db import init_db
    init_db()
    # Importing services.users runs the one-time users.json→SQLite migration
    # (idempotent + multi-process safe via BEGIN IMMEDIATE). Touch it here so the
    # DB is ready and migrated before the first request, in every worker.
    import services.users  # noqa: F401

    if not _try_become_scheduler_leader():
        logger.info("Worker %s: standby (scheduler leader already elected) — "
                    "serving requests only, background loops skipped", os.getpid())
        return
    logger.info("Worker %s: elected scheduler leader — starting background loops", os.getpid())
    asyncio.create_task(scheduler.run_scheduler())
    asyncio.create_task(autopilot.run_autopilot_credit_checker())
    asyncio.create_task(telegram.run_daily_reports())
    asyncio.create_task(automation.run_auto_dm_scheduler())
    asyncio.create_task(scheduler.run_content_refresh())
    asyncio.create_task(analytics.run_analytics_sync())
    asyncio.create_task(autopilot.run_full_auto_mode())
    asyncio.create_task(agent.run_agent_tasks())
    from services.cleanup import run_daily_cleanup
    asyncio.create_task(run_daily_cleanup())

@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "favicon.ico"), media_type="image/x-icon")

@app.get("/favicon.svg", include_in_schema=False)
def favicon_svg():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "favicon.svg"), media_type="image/svg+xml")

@app.get("/apple-touch-icon.png", include_in_schema=False)
def apple_touch_icon():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "apple-touch-icon.png"), media_type="image/png")

@app.get("/favicon-192.png", include_in_schema=False)
def favicon_192():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "frontend", "favicon-192.png"), media_type="image/png")



