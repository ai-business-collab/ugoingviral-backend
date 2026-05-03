from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime
from services.store import store, save_store, add_log
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()


# ── Playwright ────────────────────────────────────────────────────────────────
@router.get("/api/automation/playwright/check")
async def check_playwright():
    try:
        from playwright.async_api import async_playwright
        return {"installed": True, "message": "Playwright installeret ✅"}
    except ImportError:
        return {"installed": False, "message": "Kør: pip install playwright && playwright install chromium"}

@router.post("/api/automation/playwright/post")
async def playwright_post(req: PlaywrightPost, background_tasks: BackgroundTasks):
    s = store.get("settings", {})

    # Niveau 1 — brug officiel Instagram API hvis forbundet
    if req.platform == "instagram" and s.get("instagram_api_connected"):
        add_log(f"📸 Instagram API posting...", "info")
        try:
            from instagram_api import post_to_instagram, refresh_token_if_needed
            token = s.get("instagram_api_token", "")
            ig_id = s.get("instagram_ig_id", "")
            expires_at = s.get("instagram_api_expires")
            token, new_exp = await refresh_token_if_needed(token, expires_at)
            if new_exp:
                s["instagram_api_token"] = token
                s["instagram_api_expires"] = new_exp
                save_store()
            img_url = req.image_url if isinstance(req.image_url, str) else None
            img_urls = req.image_url if isinstance(req.image_url, list) else None
            result = await post_to_instagram(ig_id, token, req.content, image_url=img_url, image_urls=img_urls)
            if result.get("status") == "published":
                add_log(f"✅ Instagram API: postet! ID: {result.get('id')}", "success")
                return result
            else:
                add_log(f"⚠️ Instagram API fejl: {result.get('message')} — falder tilbage til Playwright", "warning")
        except Exception as e:
            add_log(f"⚠️ Instagram API exception: {str(e)[:80]} — falder tilbage til Playwright", "warning")

    # Niveau 2 — Playwright fallback
    user = s.get(f"{req.platform}_user", "")
    pwd = s.get(f"{req.platform}_pass", "")
    import os
    _backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    session_path = os.path.join(_backend_dir, "sessions", f"{req.platform}_session.json")
    has_session = os.path.exists(session_path)
    if not user or not pwd:
        if not has_session:
            return {"status": "error", "message": f"Tilføj login til {req.platform} under 🔗 Connect"}
        user = user or "session"
        pwd = pwd or "session"
    add_log(f"🤖 Starter {req.platform} browser automation... (content: {req.content[:30] if req.content else 'ingen'})", "info")
    background_tasks.add_task(_pw_post, req.platform, req.content, req.image_url, user, pwd)
    return {"status": "queued", "message": f"Browser åbner for {req.platform}..."}

async def _pw_post(platform, content, image_url, username, password):
    """Launch playwright as completely separate Python process"""
    import subprocess, sys, json, tempfile, os, threading

    # Load the script template
    # _pw_template.py ligger i backend/ mappen (parent af routes/)
    _backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_template_path = os.path.join(_backend_dir, "_pw_template.py")
    
    # Build script by substituting values safely
    import re
    script = open(script_template_path, encoding="utf-8").read()
    
    # Brug json.dumps til sikker string encoding - håndterer emojis og special chars
    script = script.replace("__PLATFORM__", json.dumps(platform)[1:-1])
    script = script.replace("__USERNAME__", json.dumps(username)[1:-1])
    script = script.replace("__PASSWORD__", json.dumps(password)[1:-1])
    script = script.replace("__CONTENT__", json.dumps(content)[1:-1])
    # Støtter både enkelt URL (str) og liste af URLs
    if isinstance(image_url, list):
        images_str = "|||".join([u for u in image_url if u])
    else:
        images_str = image_url or ""
    script = script.replace("__IMAGE_URLS__", json.dumps(images_str)[1:-1])

    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8')
    tmp.write(script)
    tmp.close()

    add_log(f"🌐 Åbner browser for {platform}...", "info")

    import os as _os
    _env = dict(_os.environ)
    _env["DISPLAY"] = ":99"
    _env["HOME"] = "/root"
    _env["PLAYWRIGHT_BROWSERS_PATH"] = "/root/.cache/ms-playwright"
    _env["PATH"] = "/opt/ugoingviral/bin:/usr/bin:/bin:" + _env.get("PATH", "")
    _python = "/opt/ugoingviral/bin/python3"
    if not _os.path.exists(_python):
        _python = sys.executable

    try:
        result = subprocess.run(
            [_python, tmp.name],
            capture_output=True, timeout=300,
            env=_env,
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        out = result.stdout.decode('utf-8', errors='replace').strip()
        err = result.stderr.decode('utf-8', errors='replace').strip()
        if out:
            for line in out.split("\n"):
                if line.strip():
                    add_log(f"📋 {line}", "info")
        if err:
            for eline in err.split("\n"):
                e2 = eline.strip()
                if e2 and len(e2) > 3 and not e2.startswith("DevTools"):
                    add_log(f"⚠️ {e2[:150]}", "warning")
        if "POSTED" in out or "TIKTOK_POSTED" in out:
            add_log(f"✅ {platform}: opslag postet!", "success")
        elif "LOGIN_OK" in out:
            add_log(f"✅ {platform}: logget ind!", "success")
        elif not out:
            add_log(f"⚠️ {platform}: ingen output — tjek log", "warning")
    except subprocess.TimeoutExpired:
        add_log(f"⏱️ {platform} timeout efter 5 min", "warning")
    except Exception as e:
        add_log(f"❌ {platform}: {str(e)[:80]}", "error")
    finally:
        try: os.unlink(tmp.name)
        except: pass


import time as _time

def _ig_sync(page, content, image_url, username, password):
    add_log("📸 Instagram: logger ind...", "info")
    page.goto("https://www.instagram.com/accounts/login/")
    page.wait_for_load_state("networkidle"); _time.sleep(2)
    for txt in ["Tillad alle cookies", "Allow all cookies", "Accept All"]:
        try: page.click(f"text={txt}", timeout=3000); break
        except: pass
    page.fill("input[name='username']", username)
    page.fill("input[name='password']", password)
    page.click("button[type='submit']")
    page.wait_for_load_state("networkidle"); _time.sleep(4)
    for txt in ["Ikke nu", "Not Now", "Senere"]:
        try: page.click(f"text={txt}", timeout=3000); break
        except: pass
    add_log("✅ Instagram: logget ind!", "success")

def _tt_sync(page, content, image_url, username, password):
    add_log("🎵 TikTok: logger ind...", "info")
    page.goto("https://www.tiktok.com/login")
    page.wait_for_load_state("networkidle"); _time.sleep(3)
    try:
        page.click("text=Use phone / email / username", timeout=5000); _time.sleep(1)
        page.click("text=Log in with email or username", timeout=5000); _time.sleep(1)
        page.fill("input[name='username'], input[placeholder*='mail']", username)
        page.fill("input[type='password']", password)
        page.click("button[type='submit']", timeout=5000); _time.sleep(5)
    except: pass
    add_log("✅ TikTok: logget ind!", "success")

def _fb_sync(page, content, image_url, username, password):
    add_log("📘 Facebook: logger ind...", "info")
    page.goto("https://www.facebook.com/login")
    page.wait_for_load_state("networkidle"); _time.sleep(2)
    try:
        page.fill("input[name='email']", username)
        page.fill("input[name='pass']", password)
        page.click("button[name='login']")
        page.wait_for_load_state("networkidle"); _time.sleep(3)
    except: pass
    add_log("✅ Facebook: logget ind!", "success")

def _tw_sync(page, content, image_url, username, password):
    add_log("🐦 X/Twitter: logger ind...", "info")
    page.goto("https://twitter.com/login")
    page.wait_for_load_state("networkidle"); _time.sleep(2)
    try:
        page.fill("input[autocomplete='username']", username); _time.sleep(1)
        page.click("text=Next"); _time.sleep(1)
        page.fill("input[name='password']", password)
        page.click("text=Log in"); _time.sleep(3)
    except: pass
    add_log("✅ X/Twitter: logget ind!", "success")

# ══════════════════════════════════════════════════════════
# INSTAGRAM GRAPH API