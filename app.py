"""
UgoingViral — FastAPI Backend
Koden er splittet i moduler under routes/ og services/
"""
import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, Request

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(dotenv_path=_env_path)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from services.store import set_user_context, reset_user_context
from routes import settings, products, content, posts, automation, playwright, instagram, tiktok, youtube, twitter, scheduler, email, auth, billing, admin, onboarding, stats, agent, subaccounts, studio, uploads, content_engine, autopilot, telegram, growth, nexora_core, nexora_events

app = FastAPI(title="UgoingViral API v4")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

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

os.makedirs("uploads", exist_ok=True)
os.makedirs("uploads/user_media", exist_ok=True)
os.makedirs("uploads/creators", exist_ok=True)
os.makedirs("uploads/voice", exist_ok=True)
os.makedirs("uploads/final", exist_ok=True)
os.makedirs("uploads/studio", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

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

@app.get("/privacy")
def privacy_policy():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Privacy Policy - UgoingViral</title></head>
<body style="font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px">
<h1>Privacy Policy</h1>
<p>Last updated: April 2026</p>
<h2>Data We Collect</h2>
<p>UgoingViral collects information you provide when connecting your social media accounts, including access tokens required to post content on your behalf.</p>
<h2>How We Use Your Data</h2>
<p>We use your data solely to provide the services you request, including automated social media posting and content generation.</p>
<h2>Data Deletion</h2>
<p>To request deletion of your data, contact us at: support@ugoingviral.com</p>
<h2>Contact</h2>
<p>Email: support@ugoingviral.com</p>
</body></html>"""
    return HTMLResponse(content=html)

@app.get("/terms")
def terms():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Terms of Service - UgoingViral</title></head>
<body style="font-family:sans-serif;max-width:800px;margin:40px auto;padding:20px">
<h1>Terms of Service</h1>
<p>Last updated: April 2026</p>
<p>By using UgoingViral, you agree to use the service in accordance with all applicable laws and platform terms of service.</p>
<h2>Contact</h2>
<p>Email: support@ugoingviral.com</p>
</body></html>"""
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
app.include_router(content_engine.router)
app.include_router(autopilot.router)
app.include_router(telegram.router)
app.include_router(growth.router)
app.include_router(nexora_core.router)
app.include_router(nexora_events.router)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(scheduler.run_scheduler())
