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
        ignore_https_errors=True,
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
            # Med persistent profil — gå til instagram.com og tjek om vi er logget ind
            page.goto("https://www.instagram.com/")
            time.sleep(6)
            print(f"CURRENT_URL: {page.url}")
            
            # Hvis vi er på login siden — vent 60 sekunder så bruger kan logge ind manuelt
            if "accounts/login" in page.url or "login" in page.url:
                print("NEEDS_LOGIN: Vent venligst og log ind manuelt i browseren...")
                time.sleep(60)  # Giv bruger tid til at logge ind
                # Gem session efter manuel login
                try:
                    ctx.storage_state(path=session_file)
                    print("SESSION_SAVED_AFTER_MANUAL_LOGIN")
                except:
                    pass
                print("LOGIN_OK")
                time.sleep(5)
            else:
                print("ALREADY_LOGGED_IN")
                # Allerede logget ind via persistent profil
                pass
            
            # Gå til login siden kun hvis vi ikke er logget ind
            if "accounts/login" not in page.url and False:
                page.goto("https://www.instagram.com/accounts/login/")
            time.sleep(3)
            if False:
                pass
            if False:
                time.sleep(5)
            
            try:
                page.click("text=Tillad alle cookies", timeout=6000)
                time.sleep(2)
            except:
                pass
            
            try:
                page.wait_for_selector("input", timeout=8000)
            except:
                pass
            time.sleep(1)
            
            inputs = page.query_selector_all("input")
            if len(inputs) >= 2:
                inputs[0].click(click_count=3)
                inputs[0].fill(USERNAME)
                time.sleep(0.8)
                inputs[1].click(click_count=3)
                inputs[1].fill(PASSWORD)
                time.sleep(0.8)
                print("FILLED_LOGIN")
            
            try:
                page.click("button[type='submit']", timeout=3000)
            except:
                page.keyboard.press("Enter")
            time.sleep(10)
            
            for txt in ["Ikke nu", "Not Now", "Gem ikke"]:
                try:
                    page.click("text=" + txt, timeout=1500)
                    time.sleep(0.5)
                    break
                except:
                    pass
            
            time.sleep(2)
            
            for txt in ["Ikke nu", "Not Now", "Afvis"]:
                try:
                    page.click("text=" + txt, timeout=1500)
                    time.sleep(0.5)
                    break
                except:
                    pass
            
            try:
                ctx.storage_state(path=session_file)
                print("SESSION_SAVED")
            except:
                pass
            
            print("INSTAGRAM_LOGIN_OK")
            
            if CONTENT and IMAGE_URLS:
                time.sleep(2)
                
                # Parse billede URLs — kan være én eller flere (kommasepareret)
                urls = [u.strip() for u in IMAGE_URLS.split("|||") if u.strip()]
                print(f"IMAGES_COUNT: {len(urls)}")
                
                # Download alle billeder
                for url in urls:
                    path = download_and_convert(url)
                    if path:
                        tmp_files.append(path)
                
                if not tmp_files:
                    print("NO_IMAGES_DOWNLOADED")
                else:
                    # Klik + knap
                    clicked = False
                    for sel in [
                        "svg[aria-label='Nyt opslag']",
                        "svg[aria-label='New post']",
                        "[aria-label='Nyt opslag']",
                        "[aria-label='New post']",
                        "xpath=//span[text()='Opret']/..",
                        "xpath=//a[contains(@href,'/create')]",
                        "[aria-label='New Post']",
                        "[aria-label='Create']",
                        "xpath=//div[@role='menuitem']",
                        "xpath=//a[@href='/create/style/']",
                        "[aria-label='New Post']",
                        "[aria-label='Create']",
                        "xpath=//div[@role='menuitem']",
                        "xpath=//a[@href='/create/style/']",
                    ]:
                        try:
                            page.click(sel, timeout=2000)
                            clicked = True
                            print("CLICKED_PLUS: " + sel)
                            break
                        except:
                            pass
                    
                    if not clicked:
                        for txt in ["Slå op", "Post", "Opslag", "Create"]:
                            try:
                                page.click("text=" + txt, timeout=2000)
                                clicked = True
                                print("CLICKED_MENU: " + txt)
                                break
                            except:
                                pass
                    
                    time.sleep(2)
                    
                    # Klik "Slå op" i menuen
                    for txt in ["Slå op", "Opslag", "Post", "Create post"]:
                        try:
                            page.click("text=" + txt, timeout=3000)
                            time.sleep(2)
                            print("CLICKED_MENU: " + txt)
                            break
                        except:
                            pass
                    
                    time.sleep(2)
                    
                    # Upload ALLE billeder på én gang — Instagram laver automatisk carousel
                    with page.expect_file_chooser(timeout=30000) as fc_info:
                        for _btn in ["Vælg fra computer", "Select from computer", "Vælg fra din computer", "Choose from computer"]:
                            try:
                                page.click("text=" + _btn, timeout=2000)
                                break
                            except:
                                pass
                    
                    fc_info.value.set_files(tmp_files)
                    print(f"FILES_UPLOADED: {len(tmp_files)}")
                    time.sleep(4)
                    
                    # Næste 1 — beskæringsvisning
                    try:
                        page.click("text=Næste", timeout=6000)
                        print("NAESTE_1")
                        time.sleep(3)
                    except:
                        try:
                            page.click("text=Next", timeout=3000)
                            print("NEXT_1")
                            time.sleep(3)
                        except:
                            print("NAESTE_1_FAILED")

                    # Næste 2 — filter/juster visning
                    try:
                        page.click("text=Næste", timeout=6000)
                        print("NAESTE_2")
                        time.sleep(3)
                    except:
                        try:
                            page.click("text=Next", timeout=3000)
                            print("NEXT_2")
                            time.sleep(3)
                        except:
                            print("NAESTE_2_FAILED")

                    # Caption
                    caption_written = False
                    for sel in [
                        "div[aria-label*='caption']",
                        "div[aria-label*='Caption']",
                        "div[aria-label*='Skriv en billedtekst']",
                        "div[aria-label*='Write a caption']",
                        "div[contenteditable='true']",
                        "textarea"
                    ]:
                        try:
                            el = page.query_selector(sel)
                            if el:
                                el.click()
                                time.sleep(0.5)
                                el.type(CONTENT)
                                time.sleep(1)
                                print("CAPTION_ADDED")
                                caption_written = True
                                break
                        except:
                            pass
                    
                    if not caption_written:
                        print("CAPTION_FAILED")

                    # Del
                    time.sleep(2)
                    posted = False
                    for sel in [
                        "div[role='button']:has-text('Del')",
                        "div[role='button']:has-text('Share')",
                        "button:has-text('Del')",
                        "button:has-text('Share')",
                    ]:
                        try:
                            page.click(sel, timeout=4000)
                            time.sleep(3)
                            print("INSTAGRAM_POSTED")
                            posted = True
                            break
                        except:
                            pass
                    
                    if not posted:
                        try:
                            page.click("xpath=//div[text()='Del']", timeout=4000)
                            time.sleep(3)
                            print("INSTAGRAM_POSTED")
                            posted = True
                        except:
                            pass
                    
                    if not posted:
                        print("DEL_FAILED")

        elif PLATFORM == "tiktok":
            print("TIKTOK_START")
            page.goto("https://www.tiktok.com/creator-center/upload")
            time.sleep(6)
            print(f"TIKTOK_URL: {page.url}")

            # Tjek om vi er logget ind
            if "login" in page.url or "creator-center" not in page.url:
                print("TIKTOK_NEEDS_LOGIN — prøver cookie login igen")
                page.goto("https://www.tiktok.com")
                time.sleep(5)
                page.goto("https://www.tiktok.com/creator-center/upload")
                time.sleep(5)
                print(f"TIKTOK_URL2: {page.url}")

            # Gem opdateret session
            try: ctx.storage_state(path=session_file)
            except: pass

            # Upload content hvis vi har det
            if CONTENT and CONTENT != "test" and "creator-center" in page.url:
                print("TIKTOK_UPLOADING")
                time.sleep(3)

                # TikTok bruger iframe til upload — find den
                upload_frame = None
                for frame in page.frames:
                    print(f"FRAME: {frame.url[:60]}")
                    if "tiktok" in frame.url:
                        upload_frame = frame
                        break
                if not upload_frame:
                    upload_frame = page  # fallback til main page

                # Find fil upload input — prøv både main page og iframe
                if IMAGE_URLS and IMAGE_URLS not in ("__IMAGE_URLS__", ""):
                    imgs = [u.strip() for u in IMAGE_URLS.replace("|||",",").split(",") if u.strip()]
                    if imgs:
                        local_file = download_and_convert(imgs[0])
                        if local_file:
                            uploaded = False
                            # Prøv at finde input[type=file] på alle frames
                            for frame in [page] + list(page.frames):
                                try:
                                    upload_input = frame.query_selector("input[type='file']")
                                    if upload_input:
                                        upload_input.set_files(local_file)
                                        time.sleep(6)
                                        print("TIKTOK_FILE_SET")
                                        uploaded = True
                                        break
                                except: pass
                            if not uploaded:
                                print("TIKTOK_NO_UPLOAD_INPUT — venter på manuel upload")
                                time.sleep(15)

                # Caption — prøv alle frames
                time.sleep(4)
                caption_done = False
                for frame in [page] + list(page.frames):
                    for sel in [
                        "div[contenteditable='true']",
                        ".public-DraftEditor-content",
                        "div[data-text='true']",
                        "[data-e2e='caption-input']",
                        "textarea"
                    ]:
                        try:
                            el = frame.query_selector(sel)
                            if el:
                                el.click(); time.sleep(0.5)
                                el.type(CONTENT); time.sleep(1)
                                print("TIKTOK_CAPTION_OK")
                                caption_done = True
                                break
                        except: pass
                    if caption_done: break

                # Post knap — prøv alle frames
                time.sleep(3)
                posted = False
                for frame in [page] + list(page.frames):
                    for sel in [
                        "button:has-text('Post')",
                        "button:has-text('Udgiv')",
                        "button:has-text('Submit')",
                        "[data-e2e='post-button']",
                        "button[type='submit']"
                    ]:
                        try:
                            frame.click(sel, timeout=3000)
                            time.sleep(5)
                            print("TIKTOK_POSTED")
                            posted = True
                            break
                        except: pass
                    if posted: break
                if not posted:
                    print("TIKTOK_POST_BTN_NOT_FOUND")
            else:
                print("TIKTOK_SESSION_SAVED — klar til auto-post")

            print("LOGIN_OK")
            time.sleep(15)

        elif PLATFORM == "facebook":
            page.goto("https://www.facebook.com/login")
            time.sleep(3)
            try:
                page.fill("input[name='email']", USERNAME, timeout=8000)
                page.fill("input[name='pass']", PASSWORD, timeout=5000)
                page.click("button[name='login']")
                time.sleep(5)
                try: ctx.storage_state(path=session_file)
                except: pass
                print("LOGIN_OK")
            except Exception as e:
                print(f"FACEBOOK_ERR: {e}")
            time.sleep(20)

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
