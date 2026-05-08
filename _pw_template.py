import sys, time, os, tempfile
import traceback

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    print("PLAYWRIGHT_NOT_INSTALLED")
    sys.exit(1)

# Global exception handler så vi altid ser fejlen
def _handle_exc(exc_type, exc_value, exc_tb):
    print("FATAL_ERROR: " + str(exc_value))
    print("TRACEBACK: " + "".join(traceback.format_tb(exc_tb)))
    sys.stdout.flush()
sys.excepthook = _handle_exc

PLATFORM = "__PLATFORM__"
USERNAME = "__USERNAME__"
PASSWORD = "__PASSWORD__"
CONTENT  = "__CONTENT__"
IMAGE_URLS = "__IMAGE_URLS__"  # Kommasepareret liste af URLs

# Find backend mappen via en kendt fil — data.json ligger altid i backend/
import sys as _sys
_backend_dir = os.getcwd()
# Søg opad fra cwd indtil vi finder data.json eller sessions/
for _check in [_backend_dir, os.path.dirname(_backend_dir), os.path.dirname(os.path.dirname(_backend_dir))]:
    if os.path.exists(os.path.join(_check, "data.json")) or os.path.exists(os.path.join(_check, "sessions")):
        _backend_dir = _check
        break
SESSION_DIR = os.path.join(_backend_dir, "sessions")
os.makedirs(SESSION_DIR, exist_ok=True)
session_file = os.path.join(SESSION_DIR, PLATFORM + "_session.json")
print(f"START: platform={PLATFORM} session={os.path.exists(session_file)} file={session_file}")
sys.stdout.flush()

def load_session_cookies(path):
    """Konverter Cookie-Editor JSON til Playwright storage_state format"""
    import json
    try:
        data = json.loads(open(path, encoding="utf-8").read())
        # Allerede Playwright format
        if isinstance(data, dict) and "cookies" in data:
            print(f"COOKIES_PW_FORMAT: {len(data['cookies'])} cookies")
            return path
        # Cookie-Editor liste format
        if isinstance(data, list):
            pw_cookies = []
            for c in data:
                name = c.get("name","")
                value = c.get("value","")
                if not name or not value:
                    continue
                domain = c.get("domain","")
                # Playwright kræver domain uden leading dot for hostOnly
                if c.get("hostOnly") and domain.startswith("."):
                    domain = domain[1:]
                cookie = {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": c.get("path", "/"),
                    "secure": bool(c.get("secure", False)),
                    "httpOnly": bool(c.get("httpOnly", False)),
                    "sameSite": "None" if not c.get("sameSite") or c.get("sameSite") == "no_restriction" else c.get("sameSite","None").capitalize(),
                }
                # Tilføj expires — spring session cookies over (de har ingen expiry)
                exp = c.get("expirationDate") or c.get("expires")
                if exp and not c.get("session", False):
                    cookie["expires"] = int(float(exp))
                pw_cookies.append(cookie)
            pw_state = {"cookies": pw_cookies, "origins": []}
            converted_path = path.replace(".json", "_pw.json")
            open(converted_path, "w", encoding="utf-8").write(json.dumps(pw_state))
            print(f"COOKIES_CONVERTED: {len(pw_cookies)} cookies klar")
            return converted_path
    except Exception as e:
        print(f"COOKIE_LOAD_ERR: {e}")
    return None

def download_and_convert(url):
    """Download og konverter billede til JPG. Returnerer lokal sti."""
    import urllib.request
    tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
    tmp.close()
    try:
        if url.startswith("http://localhost"):
            local_path = url.replace("http://localhost:8000/uploads/", "")
            src = os.path.join(os.getcwd(), "uploads", local_path)
            import shutil
            shutil.copy2(src, tmp.name)
        else:
            urllib.request.urlretrieve(url, tmp.name)
        
        if not url.lower().split("?")[0].endswith(('.jpg', '.jpeg', '.png')):
            try:
                from PIL import Image as _PIL
                converted = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                converted.close()
                img = _PIL.open(tmp.name)
                if img.mode in ('RGBA', 'P', 'LA'):
                    img = img.convert('RGB')
                img.save(converted.name, 'JPEG', quality=95)
                os.unlink(tmp.name)
                print("CONVERTED_TO_JPG")
                return converted.name
            except Exception as e:
                print(f"CONVERT_ERR: {e}")
        return tmp.name
    except Exception as e:
        print(f"DOWNLOAD_ERR: {e}")
        return None

with sync_playwright() as p:
    # Brug persistent Chrome profil — Instagram genkender rigtig Chrome
    import tempfile as _tf
    _profile_dir = os.path.join(SESSION_DIR, f"{PLATFORM}_chrome_profile")
    os.makedirs(_profile_dir, exist_ok=True)

    # Oxylabs residential proxy — sticky session for 1440 minutes (24h)
    _oxy_session = os.getenv("OXYLABS_SESSION_ID") or f"ugv_{PLATFORM[:8]}_{int(time.time())}"
    _proxy = {
        "server": "pr.oxylabs.io:7777",
        "username": f"customer-ugoingviral_1reRN-sessid-{_oxy_session}-sesstime-1440",
        "password": os.getenv("OXYLABS_PASSWORD", "Ugoingviral2026:"),
    }

    ctx = p.chromium.launch_persistent_context(
        _profile_dir,
        headless=True,
        slow_mo=80,
        proxy=_proxy,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--lang=da-DK",
        ],
        viewport={"width":1280,"height":900},
        locale="da-DK",
        timezone_id="Europe/Copenhagen",
        extra_http_headers={"Accept-Language": "da-DK,da;q=0.9,en;q=0.8"},
        ignore_https_errors=True,
    )
    browser = None  # ikke brugt med persistent context
    print("PERSISTENT_CONTEXT_LOADED")
    ctx.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['da-DK','da','en']});
        window.chrome = {runtime: {}};
    """)

    page = ctx.new_page()
    tmp_files = []
    print(f"PAGE_OPENED: {PLATFORM}")

    try:
        if PLATFORM == "instagram":
            print("INSTAGRAM_START")

            # ── 1. TJEK SESSION ─────────────────────────────────────────────
            page.goto("https://www.instagram.com/")
            time.sleep(7)
            print(f"INSTAGRAM_URL: {page.url}")

            # Cookie consent
            for txt in ["Tillad alle cookies", "Accept all", "Allow all cookies"]:
                try:
                    page.click(f"text={txt}", timeout=3000)
                    time.sleep(1)
                    break
                except: pass

            # Login hvis nødvendigt
            if "accounts/login" in page.url or "login" in page.url:
                print("INSTAGRAM_NEEDS_LOGIN: venter 90 sek på manuel login...")
                time.sleep(90)
                try:
                    ctx.storage_state(path=session_file)
                    print("INSTAGRAM_SESSION_SAVED_AFTER_MANUAL_LOGIN")
                except: pass

            print(f"INSTAGRAM_AFTER_LOGIN_CHECK: {page.url}")

            # Afvis popups
            for txt in ["Ikke nu", "Not Now", "Gem ikke", "Afvis"]:
                try:
                    page.click(f"text={txt}", timeout=2000)
                    time.sleep(0.5)
                    break
                except: pass

            try:
                ctx.storage_state(path=session_file)
                print("INSTAGRAM_SESSION_SAVED")
            except: pass

            print("INSTAGRAM_LOGIN_OK")

            # ── 2. SCAN ARIA-LABELS FOR DEBUG ────────────────────────────────
            print("INSTAGRAM_SCANNING_ARIA_LABELS:")
            try:
                elements = page.query_selector_all("[aria-label]")
                for el in elements[:60]:
                    try:
                        label = el.get_attribute("aria-label") or ""
                        tag   = el.evaluate("e => e.tagName.toLowerCase()")
                        href  = el.get_attribute("href") or ""
                        if label:
                            print(f"  ARIA | {tag} | label={label!r} | href={href!r}")
                    except: pass
            except Exception as e:
                print(f"ARIA_SCAN_ERR: {e}")

            # ── 3. UPLOAD ────────────────────────────────────────────────────
            has_content = bool(CONTENT and CONTENT not in ("__CONTENT__", ""))
            has_file    = bool(IMAGE_URLS and IMAGE_URLS not in ("__IMAGE_URLS__", ""))

            if has_content and has_file:
                print("INSTAGRAM_UPLOADING")
                time.sleep(2)

                # Download billeder
                urls = [u.strip() for u in IMAGE_URLS.split("|||") if u.strip()]
                print(f"INSTAGRAM_IMAGES_COUNT: {len(urls)}")
                for url in urls:
                    p = download_and_convert(url)
                    if p:
                        tmp_files.append(p)
                        print(f"INSTAGRAM_FILE_DOWNLOADED: {p}")

                if not tmp_files:
                    print("INSTAGRAM_NO_FILES_DOWNLOADED")
                else:
                    # ── TRIN 1: Klik + / Opret-knap ─────────────────────────
                    print("INSTAGRAM_STEP1: klikker + knap")
                    clicked = False

                    # Strategi A — direkte /create URL
                    try:
                        page.goto("https://www.instagram.com/create/select/")
                        time.sleep(4)
                        print(f"INSTAGRAM_CREATE_URL: {page.url}")
                        if "create" in page.url or "select" in page.url:
                            clicked = True
                            print("INSTAGRAM_NAVIGATED_TO_CREATE")
                    except: pass

                    # Strategi B — klik aria-label selectors
                    if not clicked:
                        page.goto("https://www.instagram.com/")
                        time.sleep(5)
                        for sel in [
                            "[aria-label='New post']",
                            "[aria-label='Nyt opslag']",
                            "[aria-label='Create']",
                            "[aria-label='Opret']",
                            "svg[aria-label='New post']",
                            "svg[aria-label='Nyt opslag']",
                            "a[href='/create/select/']",
                            "xpath=//a[contains(@href,'/create')]",
                        ]:
                            try:
                                page.click(sel, timeout=2000)
                                clicked = True
                                print(f"INSTAGRAM_PLUS_CLICKED: {sel}")
                                break
                            except: pass

                    # Strategi C — klik via tekst i sidebar
                    if not clicked:
                        for txt in ["Opret", "Create", "Nyt opslag", "New post"]:
                            try:
                                page.click(f"text={txt}", timeout=2000)
                                clicked = True
                                print(f"INSTAGRAM_PLUS_TEXT: {txt}")
                                break
                            except: pass

                    if not clicked:
                        print("INSTAGRAM_PLUS_NOT_FOUND")
                    else:
                        time.sleep(3)

                        # Klik "Opslag" / "Post" i undermenu hvis det dukker op
                        for txt in ["Opslag", "Post", "Slå op", "Create post"]:
                            try:
                                page.click(f"text={txt}", timeout=2000)
                                time.sleep(2)
                                print(f"INSTAGRAM_SUBMENU: {txt}")
                                break
                            except: pass

                        time.sleep(2)

                        # ── TRIN 2: Vælg fil ─────────────────────────────────
                        print("INSTAGRAM_STEP2: vælger fil")
                        file_set = False

                        # Metode A — expect_file_chooser mens vi klikker knap
                        try:
                            with page.expect_file_chooser(timeout=8000) as fc:
                                for btn_txt in ["Vælg fra computer", "Select from computer",
                                                "Choose from computer", "Vælg fra din computer"]:
                                    try:
                                        page.click(f"text={btn_txt}", timeout=2000)
                                        print(f"INSTAGRAM_FILE_BTN: {btn_txt}")
                                        break
                                    except: pass
                            fc.value.set_files(tmp_files)
                            file_set = True
                            print(f"INSTAGRAM_FILES_SET: {len(tmp_files)} filer")
                        except Exception as e:
                            print(f"INSTAGRAM_FILE_CHOOSER_ERR: {e}")

                        # Metode B — direkte input[type=file]
                        if not file_set:
                            try:
                                inp = page.query_selector("input[type='file']")
                                if inp:
                                    inp.set_files(tmp_files)
                                    file_set = True
                                    print("INSTAGRAM_FILE_VIA_INPUT")
                            except: pass

                        if not file_set:
                            print("INSTAGRAM_FILE_SET_FAILED")
                        else:
                            time.sleep(5)

                            # ── TRIN 3: Næste × 2 ────────────────────────────
                            print("INSTAGRAM_STEP3: Næste (beskæring)")
                            for step_n, step_name in [(1, "beskæring"), (2, "filter")]:
                                next_clicked = False
                                for sel in [
                                    "button:has-text('Næste')",
                                    "button:has-text('Next')",
                                    "div[role='button']:has-text('Næste')",
                                    "div[role='button']:has-text('Next')",
                                    "xpath=//button[normalize-space()='Næste']",
                                    "xpath=//button[normalize-space()='Next']",
                                ]:
                                    try:
                                        page.click(sel, timeout=6000)
                                        time.sleep(3)
                                        print(f"INSTAGRAM_NAESTE_{step_n}: {step_name}")
                                        next_clicked = True
                                        break
                                    except: pass
                                if not next_clicked:
                                    print(f"INSTAGRAM_NAESTE_{step_n}_FAILED")

                            time.sleep(2)

                            # ── TRIN 4: Caption ───────────────────────────────
                            print("INSTAGRAM_STEP4: caption")
                            caption_written = False
                            for sel in [
                                "div[aria-label*='Skriv en billedtekst']",
                                "div[aria-label*='Write a caption']",
                                "div[aria-label*='caption' i]",
                                "div[aria-label*='Caption' i]",
                                "div[contenteditable='true']",
                                "textarea",
                            ]:
                                try:
                                    el = page.query_selector(sel)
                                    if el:
                                        el.click(); time.sleep(0.5)
                                        el.type(CONTENT, delay=20)
                                        time.sleep(1)
                                        print(f"INSTAGRAM_CAPTION_ADDED: {sel}")
                                        caption_written = True
                                        break
                                except: pass

                            if not caption_written:
                                print("INSTAGRAM_CAPTION_FAILED")

                            time.sleep(2)

                            # ── TRIN 5: Del / Share ───────────────────────────
                            print("INSTAGRAM_STEP5: del/share")
                            posted = False
                            for sel in [
                                "div[role='button']:has-text('Del')",
                                "div[role='button']:has-text('Share')",
                                "button:has-text('Del')",
                                "button:has-text('Share')",
                                "xpath=//div[@role='button' and normalize-space()='Del']",
                                "xpath=//div[@role='button' and normalize-space()='Share']",
                                "xpath=//button[normalize-space()='Del']",
                                "xpath=//button[normalize-space()='Share']",
                            ]:
                                try:
                                    page.click(sel, timeout=5000)
                                    time.sleep(5)
                                    print(f"INSTAGRAM_POSTED: {sel}")
                                    posted = True
                                    break
                                except: pass

                            if not posted:
                                print("INSTAGRAM_DEL_FAILED")

                            # Gem session
                            try:
                                ctx.storage_state(path=session_file)
                                print("INSTAGRAM_SESSION_UPDATED")
                            except: pass

            else:
                print("INSTAGRAM_LOGIN_ONLY — session gemt, klar til upload")

        elif PLATFORM == "tiktok":
            print("TIKTOK_START")

            # ── 1. TJEK SESSION ─────────────────────────────────────────────
            page.goto("https://www.tiktok.com/creator-center/upload")
            time.sleep(7)
            print(f"TIKTOK_URL: {page.url}")

            needs_login = "login" in page.url or "accounts" in page.url or "creator-center" not in page.url

            # ── 2. LOGIN MED BRUGERNAVN + KODE ──────────────────────────────
            if needs_login:
                print("TIKTOK_LOGIN_REQUIRED")
                page.goto("https://www.tiktok.com/login/phone-or-email/email")
                time.sleep(5)

                # Cookie consent
                for txt in ["Accepter alle", "Accept all", "Tillad alle", "Allow all", "Accepter"]:
                    try:
                        page.click(f"text={txt}", timeout=2000)
                        time.sleep(1)
                        break
                    except: pass

                # Udfyld brugernavn / email
                try:
                    page.wait_for_selector("input[name='username'], input[type='text']", timeout=8000)
                except: pass

                for sel in [
                    "input[name='username']",
                    "input[placeholder*='email' i]",
                    "input[placeholder*='phone' i]",
                    "input[type='text']",
                ]:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click(); time.sleep(0.4)
                            el.triple_click()
                            el.fill(USERNAME)
                            print("TIKTOK_USERNAME_FILLED")
                            break
                    except: pass

                time.sleep(0.8)

                # Udfyld kode
                for sel in ["input[name='password']", "input[type='password']"]:
                    try:
                        el = page.query_selector(sel)
                        if el:
                            el.click(); time.sleep(0.4)
                            el.fill(PASSWORD)
                            print("TIKTOK_PASSWORD_FILLED")
                            break
                    except: pass

                time.sleep(0.8)

                # Klik log ind
                logged_in = False
                for sel in [
                    "button[type='submit']",
                    "button:has-text('Log in')",
                    "button:has-text('Log ind')",
                    "button:has-text('Login')",
                ]:
                    try:
                        page.click(sel, timeout=3000)
                        print("TIKTOK_LOGIN_SUBMITTED")
                        logged_in = True
                        break
                    except: pass

                if not logged_in:
                    page.keyboard.press("Enter")
                    print("TIKTOK_LOGIN_ENTER")

                # Vent — captcha kan dukke op første gang
                print("TIKTOK_WAITING_LOGIN: captcha kan kræve manuel løsning...")
                time.sleep(14)

                if "login" in page.url:
                    print("TIKTOK_POSSIBLE_CAPTCHA: venter 40 sek på manuel løsning...")
                    time.sleep(40)

                print(f"TIKTOK_AFTER_LOGIN: {page.url}")

                # Afvis popups
                for txt in ["Ikke nu", "Not Now", "Skip", "Later", "Måske senere", "Afvis"]:
                    try:
                        page.click(f"text={txt}", timeout=1500)
                        time.sleep(0.5)
                        break
                    except: pass

                # Gem session
                try:
                    ctx.storage_state(path=session_file)
                    print("TIKTOK_SESSION_SAVED")
                except: pass

                # Gå til upload
                page.goto("https://www.tiktok.com/creator-center/upload")
                time.sleep(7)
                print(f"TIKTOK_UPLOAD_URL: {page.url}")
            else:
                print("TIKTOK_ALREADY_LOGGED_IN")
                try:
                    ctx.storage_state(path=session_file)
                except: pass

            # ── 3. UPLOAD ───────────────────────────────────────────────────
            has_content   = bool(CONTENT and CONTENT not in ("__CONTENT__", ""))
            has_file      = bool(IMAGE_URLS and IMAGE_URLS not in ("__IMAGE_URLS__", ""))
            on_upload_page = "creator-center" in page.url

            if has_content and has_file and on_upload_page:
                print("TIKTOK_UPLOADING")
                time.sleep(5)

                # Download første fil (video eller billede)
                urls = [u.strip() for u in IMAGE_URLS.replace("|||", ",").split(",") if u.strip()]
                local_file = None
                if urls:
                    local_file = download_and_convert(urls[0])
                    if local_file:
                        tmp_files.append(local_file)

                if not local_file:
                    print("TIKTOK_NO_FILE_DOWNLOADED")
                else:
                    # TikTok Creator Center bruger nested iframes — scan alle
                    uploaded = False
                    for attempt in range(3):
                        for frame in [page] + list(page.frames):
                            try:
                                inp = frame.query_selector("input[type='file']")
                                if inp:
                                    inp.set_files(local_file)
                                    time.sleep(10)
                                    print(f"TIKTOK_FILE_UPLOADED: {os.path.basename(local_file)}")
                                    uploaded = True
                                    break
                            except: pass
                        if uploaded: break
                        print(f"TIKTOK_UPLOAD_ATTEMPT_{attempt+1}_RETRY")
                        time.sleep(5)

                    if not uploaded:
                        print("TIKTOK_UPLOAD_INPUT_NOT_FOUND")
                    else:
                        # Vent på at TikTok behandler filen
                        print("TIKTOK_PROCESSING_FILE")
                        time.sleep(12)

                        # Caption + hashtags — scan alle frames
                        caption_done = False
                        for frame in [page] + list(page.frames):
                            for sel in [
                                "[data-e2e='caption-input']",
                                ".public-DraftEditor-content",
                                "div[contenteditable='true']",
                                "div[data-text='true']",
                                ".editor-kit-container",
                                "div[class*='caption']",
                                "textarea",
                            ]:
                                try:
                                    el = frame.query_selector(sel)
                                    if el:
                                        el.click(); time.sleep(0.5)
                                        el.press("Control+a"); time.sleep(0.2)
                                        el.type(CONTENT, delay=25)
                                        time.sleep(1)
                                        print("TIKTOK_CAPTION_ADDED")
                                        caption_done = True
                                        break
                                except: pass
                            if caption_done: break

                        if not caption_done:
                            print("TIKTOK_CAPTION_FAILED")

                        time.sleep(3)

                        # Post knap — scan alle frames
                        posted = False
                        for frame in [page] + list(page.frames):
                            for sel in [
                                "[data-e2e='post-button']",
                                "button:has-text('Post')",
                                "button:has-text('Udgiv')",
                                "button:has-text('Publicer')",
                                "button:has-text('Submit')",
                                "div[role='button']:has-text('Post')",
                            ]:
                                try:
                                    frame.click(sel, timeout=4000)
                                    time.sleep(6)
                                    print("TIKTOK_POSTED")
                                    posted = True
                                    break
                                except: pass
                            if posted: break

                        if not posted:
                            print("TIKTOK_POST_BTN_NOT_FOUND")

                        # Gem opdateret session
                        try:
                            ctx.storage_state(path=session_file)
                            print("TIKTOK_SESSION_UPDATED")
                        except: pass

            elif not on_upload_page:
                print("TIKTOK_NOT_ON_UPLOAD_PAGE — login fejlede muligvis")
            else:
                print("TIKTOK_LOGIN_ONLY — session gemt, klar til upload")

            print("LOGIN_OK")
            time.sleep(10)

        elif PLATFORM == "youtube":
            print("YOUTUBE_START")

            # ── 1. TJEK SESSION ─────────────────────────────────────────────
            page.goto("https://studio.youtube.com")
            time.sleep(8)
            print(f"YOUTUBE_URL: {page.url}")

            needs_login = "accounts.google.com" in page.url or "signin" in page.url or "studio.youtube.com" not in page.url

            # ── 2. LOGIN VIA GOOGLE ─────────────────────────────────────────
            if needs_login:
                print("YOUTUBE_LOGIN_REQUIRED")
                page.goto("https://accounts.google.com/signin/v2/identifier?continue=https://studio.youtube.com")
                time.sleep(5)

                # Cookie consent
                for txt in ["Accepter alle", "Accept all", "Agree", "Accepter"]:
                    try:
                        page.click(f"text={txt}", timeout=2000)
                        time.sleep(1)
                        break
                    except: pass

                # Email / brugernavn
                try:
                    page.wait_for_selector("input[type='email']", timeout=10000)
                except: pass

                try:
                    el = page.query_selector("input[type='email']")
                    if el:
                        el.click(); time.sleep(0.4)
                        el.fill(USERNAME)
                        print("YOUTUBE_EMAIL_FILLED")
                except: pass

                time.sleep(0.8)

                # Næste
                for sel in ["button:has-text('Next')", "button:has-text('Næste')", "#identifierNext", "button[type='submit']"]:
                    try:
                        page.click(sel, timeout=3000)
                        print("YOUTUBE_EMAIL_NEXT")
                        break
                    except: pass

                time.sleep(4)

                # Kode
                try:
                    page.wait_for_selector("input[type='password']", timeout=10000)
                except: pass

                try:
                    el = page.query_selector("input[type='password']")
                    if el:
                        el.click(); time.sleep(0.4)
                        el.fill(PASSWORD)
                        print("YOUTUBE_PASSWORD_FILLED")
                except: pass

                time.sleep(0.8)

                # Log ind
                for sel in ["button:has-text('Next')", "button:has-text('Næste')", "#passwordNext", "button[type='submit']"]:
                    try:
                        page.click(sel, timeout=3000)
                        print("YOUTUBE_PASSWORD_NEXT")
                        break
                    except: pass

                # Vent — 2FA eller captcha kan dukke op
                print("YOUTUBE_WAITING_LOGIN: 2FA eller captcha kan kræve manuel løsning...")
                time.sleep(15)

                if "accounts.google.com" in page.url:
                    print("YOUTUBE_2FA_WAIT: venter 60 sek på manuel godkendelse...")
                    time.sleep(60)

                print(f"YOUTUBE_AFTER_LOGIN: {page.url}")

                # Gem session
                try:
                    ctx.storage_state(path=session_file)
                    print("YOUTUBE_SESSION_SAVED")
                except: pass

                # Gå til YouTube Studio
                page.goto("https://studio.youtube.com")
                time.sleep(8)
                print(f"YOUTUBE_STUDIO_URL: {page.url}")
            else:
                print("YOUTUBE_ALREADY_LOGGED_IN")
                try:
                    ctx.storage_state(path=session_file)
                except: pass

            # ── 3. UPLOAD ───────────────────────────────────────────────────
            has_content    = bool(CONTENT and CONTENT not in ("__CONTENT__", ""))
            has_file       = bool(IMAGE_URLS and IMAGE_URLS not in ("__IMAGE_URLS__", ""))
            on_studio_page = "studio.youtube.com" in page.url

            if has_content and has_file and on_studio_page:
                print("YOUTUBE_UPLOADING")

                # Download fil
                urls = [u.strip() for u in IMAGE_URLS.replace("|||", ",").split(",") if u.strip()]
                local_file = None
                if urls:
                    local_file = download_and_convert(urls[0])
                    if local_file:
                        tmp_files.append(local_file)

                if not local_file:
                    print("YOUTUBE_NO_FILE_DOWNLOADED")
                else:
                    # Klik Upload-knap i Studio topbar
                    upload_clicked = False
                    for sel in [
                        "ytcp-button#upload-icon",
                        "button[aria-label*='Upload' i]",
                        "button[aria-label*='Opret' i]",
                        "#create-icon",
                        "ytcp-icon-button#upload-icon",
                        "tp-yt-paper-icon-button[id='upload-icon']",
                    ]:
                        try:
                            page.click(sel, timeout=4000)
                            time.sleep(2)
                            print(f"YOUTUBE_UPLOAD_BTN: {sel}")
                            upload_clicked = True
                            break
                        except: pass

                    if not upload_clicked:
                        # Prøv tekst-baseret klik
                        for txt in ["Upload videos", "Upload videoer", "Opret", "Create"]:
                            try:
                                page.click(f"text={txt}", timeout=3000)
                                time.sleep(2)
                                print(f"YOUTUBE_UPLOAD_TXT: {txt}")
                                upload_clicked = True
                                break
                            except: pass

                    if not upload_clicked:
                        print("YOUTUBE_UPLOAD_BTN_NOT_FOUND")
                    else:
                        time.sleep(2)

                        # Vælg fil via input eller "Upload video" menuitem
                        for txt in ["Upload video", "Upload videos", "Upload fil"]:
                            try:
                                page.click(f"text={txt}", timeout=2000)
                                time.sleep(1)
                                break
                            except: pass

                        # Sæt filen via input[type=file]
                        file_set = False
                        for attempt in range(3):
                            for frame in [page] + list(page.frames):
                                try:
                                    inp = frame.query_selector("input[type='file']")
                                    if inp:
                                        inp.set_files(local_file)
                                        time.sleep(3)
                                        print(f"YOUTUBE_FILE_SET: {os.path.basename(local_file)}")
                                        file_set = True
                                        break
                                except: pass
                            if file_set: break
                            time.sleep(4)

                        if not file_set:
                            # Prøv expect_file_chooser
                            try:
                                with page.expect_file_chooser(timeout=8000) as fc:
                                    for sel in ["[class*='file-picker']", "label[for*='file']", "ytcp-uploads-file-picker"]:
                                        try:
                                            page.click(sel, timeout=2000)
                                            break
                                        except: pass
                                fc.value.set_files(local_file)
                                print("YOUTUBE_FILE_VIA_CHOOSER")
                                file_set = True
                                time.sleep(3)
                            except: pass

                        if not file_set:
                            print("YOUTUBE_FILE_INPUT_NOT_FOUND")
                        else:
                            # Vent på upload dialog åbner
                            print("YOUTUBE_WAITING_DIALOG")
                            time.sleep(6)

                            # Titel — ryd og skriv nyt
                            title_done = False
                            for sel in [
                                "#title-textarea #child-input",
                                "ytcp-mention-textbox[label='Title'] #textbox",
                                "div#textbox[aria-label*='title' i]",
                                "div#textbox[aria-label*='titel' i]",
                                "#title-textarea div[contenteditable]",
                            ]:
                                try:
                                    el = page.query_selector(sel)
                                    if el:
                                        el.click(); time.sleep(0.3)
                                        el.press("Control+a")
                                        time.sleep(0.2)
                                        # Titel = første linje af CONTENT, max 100 tegn
                                        title = CONTENT.split("\n")[0][:100]
                                        el.type(title, delay=20)
                                        time.sleep(0.5)
                                        print("YOUTUBE_TITLE_ADDED")
                                        title_done = True
                                        break
                                except: pass

                            if not title_done:
                                print("YOUTUBE_TITLE_FAILED")

                            # Beskrivelse
                            desc_done = False
                            for sel in [
                                "#description-textarea #child-input",
                                "ytcp-mention-textbox[label='Description'] #textbox",
                                "div#textbox[aria-label*='description' i]",
                                "div#textbox[aria-label*='beskrivelse' i]",
                                "#description-textarea div[contenteditable]",
                            ]:
                                try:
                                    el = page.query_selector(sel)
                                    if el:
                                        el.click(); time.sleep(0.3)
                                        el.press("Control+a")
                                        time.sleep(0.2)
                                        el.type(CONTENT, delay=15)
                                        time.sleep(0.5)
                                        print("YOUTUBE_DESC_ADDED")
                                        desc_done = True
                                        break
                                except: pass

                            if not desc_done:
                                print("YOUTUBE_DESC_FAILED")

                            # Short — marker som Short hvis videoen er vertikal
                            time.sleep(2)

                            # Næste → Næste → Næste → Publicer (3 steps i wizard)
                            for step in range(3):
                                for sel in [
                                    "ytcp-button#next-button",
                                    "button:has-text('Next')",
                                    "button:has-text('Næste')",
                                    "#next-button",
                                ]:
                                    try:
                                        page.click(sel, timeout=5000)
                                        time.sleep(3)
                                        print(f"YOUTUBE_NEXT_{step+1}")
                                        break
                                    except: pass

                            # Publicer
                            published = False
                            for sel in [
                                "ytcp-button#done-button",
                                "button:has-text('Publish')",
                                "button:has-text('Publicer')",
                                "button:has-text('Save')",
                                "#done-button",
                            ]:
                                try:
                                    page.click(sel, timeout=6000)
                                    time.sleep(6)
                                    print("YOUTUBE_PUBLISHED")
                                    published = True
                                    break
                                except: pass

                            if not published:
                                print("YOUTUBE_PUBLISH_BTN_NOT_FOUND")

                            # Gem opdateret session
                            try:
                                ctx.storage_state(path=session_file)
                                print("YOUTUBE_SESSION_UPDATED")
                            except: pass

            elif not on_studio_page:
                print("YOUTUBE_NOT_ON_STUDIO — login fejlede muligvis")
            else:
                print("YOUTUBE_LOGIN_ONLY — session gemt, klar til upload")

            print("LOGIN_OK")
            time.sleep(10)

        elif PLATFORM == "facebook":
            print("FACEBOOK_START")

            # ── 1. TJEK SESSION ─────────────────────────────────────────────
            page.goto("https://www.facebook.com")
            time.sleep(7)
            print(f"FACEBOOK_URL: {page.url}")

            needs_login = "login" in page.url or "facebook.com/login" in page.url or page.query_selector("input[name='email']") is not None

            # ── 2. LOGIN MED EMAIL + KODE ────────────────────────────────────
            if needs_login:
                print("FACEBOOK_LOGIN_REQUIRED")
                page.goto("https://www.facebook.com/login")
                time.sleep(5)

                # Cookie consent
                for txt in ["Tillad alle cookies", "Accept all", "Accepter alle", "Allow all cookies", "OK"]:
                    try:
                        page.click(f"text={txt}", timeout=2000)
                        time.sleep(1)
                        break
                    except: pass

                # Email
                try:
                    page.wait_for_selector("input[name='email']", timeout=8000)
                    el = page.query_selector("input[name='email']")
                    if el:
                        el.click(); time.sleep(0.4)
                        el.fill(USERNAME)
                        print("FACEBOOK_EMAIL_FILLED")
                except: pass

                time.sleep(0.6)

                # Kode
                try:
                    el = page.query_selector("input[name='pass']")
                    if el:
                        el.click(); time.sleep(0.4)
                        el.fill(PASSWORD)
                        print("FACEBOOK_PASSWORD_FILLED")
                except: pass

                time.sleep(0.6)

                # Log ind
                for sel in ["button[name='login']", "button[type='submit']", "input[type='submit']"]:
                    try:
                        page.click(sel, timeout=3000)
                        print("FACEBOOK_LOGIN_SUBMITTED")
                        break
                    except: pass

                # Vent — 2FA eller checkpoint kan dukke op
                print("FACEBOOK_WAITING_LOGIN: 2FA/checkpoint kan kræve manuel løsning...")
                time.sleep(12)

                if "checkpoint" in page.url or "login" in page.url:
                    print("FACEBOOK_CHECKPOINT: venter 60 sek på manuel godkendelse...")
                    time.sleep(60)

                print(f"FACEBOOK_AFTER_LOGIN: {page.url}")

                # Afvis popups
                for txt in ["Ikke nu", "Not Now", "Luk", "Close", "Afvis"]:
                    try:
                        page.click(f"text={txt}", timeout=1500)
                        time.sleep(0.5)
                        break
                    except: pass

                # Gem session
                try:
                    ctx.storage_state(path=session_file)
                    print("FACEBOOK_SESSION_SAVED")
                except: pass
            else:
                print("FACEBOOK_ALREADY_LOGGED_IN")
                try:
                    ctx.storage_state(path=session_file)
                except: pass

            # ── 3. OPRET OPSLAG ─────────────────────────────────────────────
            has_content  = bool(CONTENT and CONTENT not in ("__CONTENT__", ""))
            has_file     = bool(IMAGE_URLS and IMAGE_URLS not in ("__IMAGE_URLS__", ""))
            on_fb        = "facebook.com" in page.url and "login" not in page.url

            if has_content and on_fb:
                print("FACEBOOK_CREATING_POST")

                # Naviger til brugerens feed / side
                page.goto("https://www.facebook.com")
                time.sleep(5)

                # Download billede hvis der er et
                local_file = None
                if has_file:
                    urls = [u.strip() for u in IMAGE_URLS.replace("|||", ",").split(",") if u.strip()]
                    if urls:
                        local_file = download_and_convert(urls[0])
                        if local_file:
                            tmp_files.append(local_file)

                # Klik "Hvad tænker du på?" / "What's on your mind?"
                post_box_clicked = False
                for sel in [
                    "div[aria-label*='tænker' i]",
                    "div[aria-label*=\"What's on your mind\" i]",
                    "div[role='button'][aria-label*='opslag' i]",
                    "span:has-text(\"Hvad tænker du på\")",
                    "span:has-text(\"What's on your mind\")",
                ]:
                    try:
                        page.click(sel, timeout=4000)
                        time.sleep(2)
                        print(f"FACEBOOK_POSTBOX_CLICKED: {sel}")
                        post_box_clicked = True
                        break
                    except: pass

                if not post_box_clicked:
                    print("FACEBOOK_POSTBOX_NOT_FOUND")
                else:
                    time.sleep(2)

                    # Skriv tekst i dialog
                    text_done = False
                    for sel in [
                        "div[aria-label*='tænker' i][contenteditable]",
                        "div[aria-label*=\"What's on your mind\" i][contenteditable]",
                        "div[contenteditable='true'][role='textbox']",
                        "div[contenteditable='true']",
                    ]:
                        try:
                            el = page.query_selector(sel)
                            if el:
                                el.click(); time.sleep(0.4)
                                el.type(CONTENT, delay=20)
                                time.sleep(1)
                                print("FACEBOOK_TEXT_ADDED")
                                text_done = True
                                break
                        except: pass

                    if not text_done:
                        print("FACEBOOK_TEXT_FAILED")

                    # Vedhæft billede hvis tilgængeligt
                    if local_file:
                        time.sleep(2)
                        img_attached = False

                        # Klik Foto/Video knap i composer
                        for sel in [
                            "div[aria-label='Foto/video']",
                            "div[aria-label='Photo/video']",
                            "div[aria-label*='Foto' i]",
                            "div[aria-label*='Photo' i]",
                            "input[type='file']",
                        ]:
                            try:
                                el = page.query_selector(sel)
                                if el:
                                    if sel == "input[type='file']":
                                        el.set_files(local_file)
                                    else:
                                        el.click()
                                        time.sleep(2)
                                        # Find input efter klik
                                        inp = page.query_selector("input[type='file']")
                                        if inp:
                                            inp.set_files(local_file)
                                    time.sleep(4)
                                    print("FACEBOOK_IMAGE_ATTACHED")
                                    img_attached = True
                                    break
                            except: pass

                        if not img_attached:
                            # Prøv expect_file_chooser
                            try:
                                with page.expect_file_chooser(timeout=6000) as fc:
                                    for sel in ["div[aria-label*='Foto' i]", "div[aria-label*='Photo' i]", "span:has-text('Foto')"]:
                                        try:
                                            page.click(sel, timeout=2000)
                                            break
                                        except: pass
                                fc.value.set_files(local_file)
                                time.sleep(4)
                                print("FACEBOOK_IMAGE_VIA_CHOOSER")
                                img_attached = True
                            except: pass

                        if not img_attached:
                            print("FACEBOOK_IMAGE_FAILED — poster uden billede")

                    time.sleep(2)

                    # Publicer
                    published = False
                    for sel in [
                        "div[aria-label='Opret opslag']",
                        "div[aria-label='Post']",
                        "button[type='submit']:has-text('Opret opslag')",
                        "button:has-text('Opret opslag')",
                        "button:has-text('Post')",
                        "div[role='button']:has-text('Opret opslag')",
                        "div[role='button']:has-text('Post')",
                    ]:
                        try:
                            page.click(sel, timeout=4000)
                            time.sleep(5)
                            print("FACEBOOK_POSTED")
                            published = True
                            break
                        except: pass

                    if not published:
                        print("FACEBOOK_POST_BTN_NOT_FOUND")

                    # Gem opdateret session
                    try:
                        ctx.storage_state(path=session_file)
                        print("FACEBOOK_SESSION_UPDATED")
                    except: pass

            elif not on_fb:
                print("FACEBOOK_NOT_LOGGED_IN — login fejlede muligvis")
            else:
                print("FACEBOOK_LOGIN_ONLY — session gemt, klar til upload")

            print("LOGIN_OK")
            time.sleep(10)

        elif PLATFORM == "twitter":
            page.goto("https://twitter.com/login")
            time.sleep(3)
            try:
                page.fill("input[autocomplete='username']", USERNAME, timeout=8000)
                time.sleep(1)
                page.click("text=Next")
                time.sleep(2)
                page.fill("input[name='password']", PASSWORD, timeout=5000)
                page.click("text=Log in")
                time.sleep(5)
                try: ctx.storage_state(path=session_file)
                except: pass
                print("LOGIN_OK")
            except Exception as e:
                print(f"TWITTER_ERR: {e}")
            time.sleep(20)

        # Gem session
        try: ctx.storage_state(path=session_file)
        except: pass
        time.sleep(10)

    except Exception as e:
        print("ERROR: " + str(e))
    finally:
        ctx.close()
        for f in tmp_files:
            try: os.unlink(f)
            except: pass
