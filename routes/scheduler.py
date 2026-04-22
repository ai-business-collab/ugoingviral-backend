from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime, timedelta
import random
from services.store import store, save_store, add_log, load_store, get_all_user_ids, set_user_context, reset_user_context
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()

_scheduler_running = False


async def get_all_products_cached() -> list:
    manual = store.get("manual_products", [])
    shopify_cached = store.get("shopify_products_cache", [])
    if not shopify_cached:
        try:
            from routes.products import get_shopify_token, refresh_shopify_token
            token = await get_shopify_token()
            s = store.get("settings", {})
            store_url = s.get("shopify_store","").replace("https://","").replace("http://","").strip("/")
            if token and store_url:
                async with httpx.AsyncClient() as c:
                    r = await c.get(f"https://{store_url}/admin/api/2024-01/products.json?limit=250",
                        headers={"X-Shopify-Access-Token": token}, timeout=15)
                    if r.status_code == 200:
                        shopify_cached = []
                        for p in r.json().get("products", []):
                            imgs = p.get("images", [])
                            shopify_cached.append({
                                "id": str(p["id"]), "title": p["title"],
                                "description": p.get("body_html","").replace("<p>","").replace("</p>","").strip()[:300],
                                "price": p.get("variants",[{}])[0].get("price","0"),
                                "images": [i.get("src","") for i in imgs],
                                "image": imgs[0].get("src","") if imgs else "",
                                "status": p.get("status","active"), "source": "shopify",
                                "group": "Alle",
                                "content_count": len(store.get("product_content",{}).get(str(p["id"]),[]))
                            })
                        store["shopify_products_cache"] = shopify_cached
                        import time; store["shopify_cache_time"] = time.time()
                        save_store()
        except Exception:
            pass
    all_prods = manual + [p for p in shopify_cached if p["id"] not in [m["id"] for m in manual]]
    return all_prods


async def plan_next_day():
    auto = store.get("automation", {})
    if not auto.get("active", False):
        return
    tomorrow = datetime.now() + timedelta(days=1)
    weekday = tomorrow.isoweekday()
    schedule_days = auto.get("schedule_days", [1,2,3,4,5,6,7])
    if weekday not in schedule_days:
        add_log(f"📅 Ingen plan for i morgen — ikke en post-dag", "info")
        return
    post_times = auto.get("post_times", ["09:00", "14:00", "18:00"])
    date_str = tomorrow.strftime("%Y-%m-%d")
    existing = [p for p in store.get("scheduled_posts", []) if p.get("scheduled_time","").startswith(date_str) and p.get("source") == "auto_plan"]
    if existing:
        add_log(f"📅 Plan for {date_str} allerede lavet ({len(existing)} opslag)", "info")
        return
    all_products = await get_all_products_cached()
    if not all_products:
        add_log("⚠️ Ingen produkter — kan ikke lave plan", "warning")
        return
    auto_groups = auto.get("auto_groups", [])
    if auto_groups:
        products = [p for p in all_products if p.get("group","") in auto_groups] or all_products
    else:
        products = all_products
    platforms_cfg = auto.get("platforms", {})
    if isinstance(platforms_cfg, dict):
        active_platforms = [p for p, v in platforms_cfg.items() if isinstance(v, dict) and v.get("auto_post", False)]
    else:
        active_platforms = platforms_cfg
    if not active_platforms:
        add_log("⚠️ Ingen platforme med auto-post slået til", "warning")
        return
    settings_s = store.get("settings", {})
    new_posts = []
    scheduler_log = store.get("scheduler_log", {})
    used_ids = []
    for t in post_times:
        post_key = f"{date_str}_{t}"
        if post_key in scheduler_log:
            continue
        available = [p for p in products if p["id"] not in used_ids] or products
        product = random.choice(available) if auto.get("post_order","random") == "random" else sorted(available, key=lambda p: p.get("created",""), reverse=True)[0]
        used_ids.append(product["id"])
        product_content = (store.get("product_content", {}).get(str(product["id"]), []) or
                            store.get("product_content", {}).get(product["id"], []))
        captions = [c for c in product_content if c.get("type") == "caption"]
        caption_text = captions[0]["content"] if captions else f"Tjek vores {product['title']}! 🔥"
        imgs_raw = product.get("images", [])
        imgs = []
        for img in imgs_raw:
            if isinstance(img, dict):
                imgs.append(img.get("src", ""))
            elif isinstance(img, str) and img:
                imgs.append(img)
        if not imgs and product.get("image"):
            imgs = [product["image"]]
        imgs = [i for i in imgs if i]
        image_url = imgs[0] if imgs else ""
        for platform in active_platforms:
            user = settings_s.get(f"{platform}_user", "")
            if not user and not store.get("settings", {}).get("instagram_api_connected"):
                continue
            new_posts.append({
                "id": f"auto_{date_str}_{t}_{platform}",
                "platform": platform,
                "content": caption_text,
                "image_url": image_url,
                "scheduled_time": f"{date_str}T{t}",
                "product_id": str(product["id"]),
                "product_title": product["title"],
                "product_image": imgs[0] if imgs else "",
                "status": "scheduled",
                "mode": "ui",
                "source": "auto_plan",
            })
    if new_posts:
        store.setdefault("scheduled_posts", []).extend(new_posts)
        save_store()
        add_log(f"✅ Plan klar: {len(new_posts)} opslag planlagt til {date_str}", "success")
    else:
        add_log(f"⚠️ Ingen nye opslag planlagt til {date_str}", "warning")


async def _run_for_user():
    """Kør scheduler-logik for den aktuelle bruger (sat via kontekst)."""
    try:
        fresh = load_store()
        current_scheduled = store.get("scheduled_posts", [])
        store.update(fresh)

        disk_scheduled = fresh.get("scheduled_posts", [])
        posting_ids = {p["id"] for p in current_scheduled if p.get("status") == "posting"}
        if posting_ids:
            merged = [p for p in current_scheduled if p.get("status") == "posting"]
            merged += [p for p in disk_scheduled if p.get("id") not in posting_ids]
            store["scheduled_posts"] = merged
        else:
            store["scheduled_posts"] = disk_scheduled

        # ── AUTO-REPOST kl. 08:00 ──────────────────────────────────────
        now_check = datetime.now()
        if now_check.strftime("%H:%M") in ["08:00", "08:01"]:
            repost_key = f"repost_{now_check.strftime('%Y-%m-%d')}"
            rps = store.get("repost_settings", {})
            if rps.get("enabled") and repost_key not in store.get("scheduler_log", {}):
                interval = rps.get("interval_days", 7)
                max_pw = rps.get("max_per_week", 3)
                platforms = rps.get("platforms", [])
                history = store.get("history", [])
                week_reposts = sum(
                    1 for p in store.get("scheduled_posts", [])
                    if p.get("mode") == "repost" and
                    (now_check - datetime.fromisoformat(p.get("scheduled_time", now_check.isoformat()))).days < 7
                )
                if week_reposts < max_pw and history:
                    from_dt = now_check - timedelta(days=interval * 4)
                    candidates = [
                        h for h in history
                        if h.get("content") and len(h.get("content", "")) > 20
                        and datetime.fromisoformat(h.get("created_at", h.get("ts", now_check.isoformat()))) < from_dt
                    ]
                    if candidates:
                        import random as _rnd
                        pick = _rnd.choice(candidates)
                        for plat in (platforms or ["instagram"]):
                            stime = (now_check + timedelta(hours=2)).isoformat()
                            store["scheduled_posts"].insert(0, {
                                "id": now_check.isoformat() + f"_repost_{plat}",
                                "platform": plat,
                                "content": pick.get("content", ""),
                                "image_url": pick.get("image_url", ""),
                                "scheduled_time": stime,
                                "status": "scheduled",
                                "mode": "repost",
                            })
                        store.setdefault("scheduler_log", {})[repost_key] = True
                        save_store()
                        add_log(f"♻️ Auto-repost planlagt for {', '.join(platforms or ['instagram'])}", "info")

        # ── DAGLIG PLANLÆGGER kl. 20:00 ─────────────────────────────────
        if now_check.strftime("%H:%M") in ["20:00", "20:01"]:
            plan_key = f"plan_{now_check.strftime('%Y-%m-%d')}"
            if plan_key not in store.get("scheduler_log", {}):
                future_planned = 0
                for day_offset in range(1, 8):
                    future_day = (now_check + timedelta(days=day_offset)).strftime("%Y-%m-%d")
                    day_posts = [p for p in store.get("scheduled_posts", []) if p.get("scheduled_time","").startswith(future_day)]
                    if day_posts:
                        future_planned += 1
                if future_planned >= 2:
                    add_log(f"📅 Daglig planlægger: {future_planned} dage allerede planlagt — springer over", "info")
                    store.setdefault("scheduler_log", {})[plan_key] = True
                    save_store()
                else:
                    await plan_next_day()
                    store.setdefault("scheduler_log", {})[plan_key] = True
                    save_store()

        # ── PLANLAGTE OPSLAG WORKER ─────────────────────────────────────
        now_dt = datetime.now()
        scheduled = store.get("scheduled_posts", [])
        to_execute = []
        remaining = []
        for post in scheduled:
            try:
                stime = post.get("scheduled_time", "")
                if not stime:
                    remaining.append(post)
                    continue
                post_dt = datetime.fromisoformat(stime)
                if post_dt <= now_dt and post.get("status") == "scheduled":
                    to_execute.append(post)
                else:
                    remaining.append(post)
            except Exception:
                remaining.append(post)

        if to_execute:
            for post in to_execute:
                post["status"] = "posting"
            store["scheduled_posts"] = remaining
            save_store()

        for post in to_execute:
            platform = post.get("platform", "")
            post_content = post.get("content", "")
            image_url = post.get("image_url", "")
            prod_id = post.get("product_id", "")
            prod_title = post.get("title", "Opslag")
            if prod_id and not image_url:
                all_prods = store.get("manual_products", []) + store.get("shopify_products_cache", [])
                prod = next((p for p in all_prods if str(p["id"]) == str(prod_id)), None)
                if prod:
                    imgs = prod.get("images", [])
                    if not imgs and prod.get("image"):
                        imgs = [prod["image"]]
                    if len(imgs) > 1:
                        image_url = imgs
                    elif imgs:
                        image_url = imgs[0]
                    prod_title = prod.get("title", prod_title)
            settings_data = store.get("settings", {})

            if platform == "instagram" and settings_data.get("instagram_api_connected"):
                add_log(f"📅 Udfører planlagt opslag via Instagram API...", "info")
                async def _post_via_api(pc=post_content, iu=image_url, pt=prod_title, sd=settings_data):
                    try:
                        from instagram_api import post_to_instagram, refresh_token_if_needed
                        token = sd.get("instagram_api_token", "")
                        ig_id = sd.get("instagram_ig_id", "")
                        expires_at = sd.get("instagram_api_expires")
                        token, new_exp = await refresh_token_if_needed(token, expires_at)
                        img_url = iu if isinstance(iu, str) else None
                        img_urls = iu if isinstance(iu, list) else None
                        result = await post_to_instagram(ig_id, token, pc, image_url=img_url, image_urls=img_urls)
                        if result.get("status") == "published":
                            add_log(f"✅ Planlagt opslag postet via API: {pt[:25]}", "success")
                            now_s = datetime.now()
                            key = f"{now_s.strftime('%Y-%m-%d')}_{now_s.strftime('%H:%M')}_manual"
                            store.setdefault("scheduler_log", {})[key] = {
                                "product": pt, "platform": "instagram",
                                "time": now_s.strftime("%H:%M"), "date": now_s.strftime("%Y-%m-%d")
                            }
                            save_store()
                        else:
                            add_log(f"❌ API post fejl: {result.get('message','')}", "error")
                    except Exception as e:
                        add_log(f"❌ API post exception: {str(e)[:80]}", "error")
                asyncio.create_task(_post_via_api())
                continue

            user = settings_data.get(f"{platform}_user", "")
            pwd = settings_data.get(f"{platform}_pass", "")
            session_path = os.path.join(os.path.dirname(__file__), "sessions", f"{platform}_session.json")
            has_session = os.path.exists(session_path)
            if not user or not pwd:
                if not has_session:
                    add_log(f"⚠️ Planlagt opslag: ingen login til {platform}", "warning")
                    continue
                user = user or "session"
                pwd = pwd or "session"
            add_log(f"📅 Udfører planlagt opslag på {platform}: {prod_title[:25]}...", "info")
            now_s2 = datetime.now()
            key2 = f"{now_s2.strftime('%Y-%m-%d')}_{now_s2.strftime('%H:%M')}_manual"
            store.setdefault("scheduler_log", {})[key2] = {
                "product": prod_title, "platform": platform,
                "time": now_s2.strftime("%H:%M"), "date": now_s2.strftime("%Y-%m-%d")
            }
            save_store()
            asyncio.create_task(asyncio.to_thread(_pw_post_sync, platform, post_content, image_url, user, pwd))

        # ── AUTOMATION (real-time posting) ──────────────────────────────
        auto = store.get("automation", {})
        if not auto.get("active", False):
            return

        now = datetime.now()

        # ── Hvileperiode — ingen posting 23:00–07:00 (med mindre slået fra) ──
        if auto.get("respect_quiet_hours", True):
            quiet_start = auto.get("quiet_start", "23:00")
            quiet_end   = auto.get("quiet_end",   "07:00")
            try:
                qs_h, qs_m = int(quiet_start[:2]), int(quiet_start[3:])
                qe_h, qe_m = int(quiet_end[:2]),   int(quiet_end[3:])
                now_mins = now.hour * 60 + now.minute
                qs_mins  = qs_h * 60 + qs_m
                qe_mins  = qe_h * 60 + qe_m
                in_quiet = (now_mins >= qs_mins) or (now_mins < qe_mins)
                if in_quiet:
                    return
            except Exception:
                pass

        # ── Aktivitetsgrænse: max opslag per time ─────────────────────────
        max_per_hour = auto.get("max_posts_per_hour", 0)
        if max_per_hour and max_per_hour > 0:
            hour_key = now.strftime("%Y-%m-%dT%H")
            posts_this_hour = store.get("scheduler_log", {}).get(f"hourly_{hour_key}", 0)
            if posts_this_hour >= max_per_hour:
                return

        weekday = now.isoweekday()
        schedule_days = auto.get("schedule_days", [1,2,3,4,5])
        if weekday not in schedule_days:
            return

        post_times = auto.get("post_times", ["09:00", "14:00", "18:00"])
        current_time = now.strftime("%H:%M")

        time_match = False
        for pt in post_times:
            if current_time == pt:
                time_match = True
                break
            try:
                pt_obj = datetime.strptime(pt, "%H:%M")
                now_obj = datetime.strptime(current_time, "%H:%M")
                diff = abs((now_obj.hour * 60 + now_obj.minute) - (pt_obj.hour * 60 + pt_obj.minute))
                if diff <= 1:
                    time_match = True
                    break
            except Exception:
                pass

        if not time_match:
            return

        today = now.strftime("%Y-%m-%d")
        last_posts = store.get("scheduler_log", {})
        post_key = f"{today}_{current_time}"
        if post_key in last_posts:
            return

        all_products = await get_all_products_cached()
        if not all_products:
            add_log("⚠️ Ingen produkter at poste — tilslut Shopify eller tilføj manuelt", "warning")
            return

        auto_groups = auto.get("auto_groups", [])
        if auto_groups:
            products = [p for p in all_products if p.get("group", "") in auto_groups] or all_products
        else:
            products = all_products

        if auto.get("post_order", "random") == "random":
            product = random.choice(products)
        else:
            product = sorted(products, key=lambda p: p.get("created",""), reverse=True)[0]

        product_content = (store.get("product_content", {}).get(str(product["id"]), []) or
                        store.get("product_content", {}).get(product["id"], []))
        captions = [c for c in product_content if c.get("type") == "caption"]

        if auto.get("no_duplicate_days", True):
            yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
            yesterday_posts = [v for k, v in last_posts.items() if k.startswith(yesterday)]
            if any(p.get("product_id") == product["id"] for p in yesterday_posts):
                other_products = [p for p in products if p["id"] != product["id"]]
                if other_products:
                    product = random.choice(other_products)
                    product_content = (store.get("product_content", {}).get(str(product["id"]), []) or
                        store.get("product_content", {}).get(product["id"], []))
                    captions = [c for c in product_content if c.get("type") == "caption"]

        caption_text = captions[0]["content"] if captions else f"Tjek vores {product['title']}! 🔥"
        product_images = product.get("images", [])
        if not product_images and product.get("image"):
            product_images = [product["image"]]
        image_url = product_images
        if len(product_images) > 1:
            add_log(f"🖼️ {len(product_images)} billeder — {product['title'][:20]}", "info")
        elif product_images:
            image_url = product_images[0]

        platforms = auto.get("platforms", {})
        if isinstance(platforms, list):
            active_platforms = platforms
        else:
            active_platforms = [p for p, v in platforms.items() if isinstance(v, dict) and v.get("auto_post", False)]

        for platform in active_platforms:
            settings = store.get("settings", {})

            # Brug officiel API hvis forbundet — ingen browser nødvendig
            if platform == "instagram" and settings.get("instagram_api_connected"):
                add_log(f"⏰ Scheduler → Instagram API: {product['title'][:30]}", "info")
                async def _auto_ig(pc=caption_text, iu=image_url, sd=settings, pt=product["title"]):
                    try:
                        from instagram_api import post_to_instagram, refresh_token_if_needed
                        token = sd.get("instagram_api_token", "")
                        ig_id = sd.get("instagram_ig_id", "")
                        exp   = sd.get("instagram_api_expires")
                        token, new_exp = await refresh_token_if_needed(token, exp)
                        img_url  = iu if isinstance(iu, str) else None
                        img_urls = iu if isinstance(iu, list) else None
                        result = await post_to_instagram(ig_id, token, pc, image_url=img_url, image_urls=img_urls)
                        add_log(f"{'✅' if result.get('status')=='published' else '❌'} Auto Instagram API: {pt[:25]}", "success" if result.get("status") == "published" else "error")
                    except Exception as e:
                        add_log(f"❌ Auto Instagram API fejl: {str(e)[:80]}", "error")
                asyncio.create_task(_auto_ig())
                continue

            if platform == "tiktok" and settings.get("tiktok_api_connected"):
                add_log(f"⏰ Scheduler → TikTok API: {product['title'][:30]}", "info")
                async def _auto_tt(pc=caption_text, iu=image_url, sd=settings, pt=product["title"]):
                    try:
                        from tiktok_api import refresh_token_if_needed as tt_refresh, publish_to_tiktok
                        token   = sd.get("tiktok_access_token", "")
                        refresh = sd.get("tiktok_refresh_token", "")
                        exp     = sd.get("tiktok_expires_at")
                        new_token, new_ref, new_exp = await tt_refresh(token, refresh, exp)
                        if new_exp:
                            store["settings"]["tiktok_access_token"] = new_token
                            save_store()
                            token = new_token
                        vid_url = iu if isinstance(iu, str) and iu.endswith(('.mp4','.mov')) else None
                        img_url = iu if isinstance(iu, str) and not vid_url else None
                        result = await publish_to_tiktok(token, pc, video_url=vid_url, image_url=img_url)
                        add_log(f"{'✅' if result.get('status') in ('published','processing') else '❌'} Auto TikTok API: {pt[:25]}", "success" if result.get("status") in ("published","processing") else "error")
                    except Exception as e:
                        add_log(f"❌ Auto TikTok API fejl: {str(e)[:80]}", "error")
                asyncio.create_task(_auto_tt())
                continue

            user = settings.get(f"{platform}_user", "")
            pwd  = settings.get(f"{platform}_pass", "")
            if not user or not pwd:
                add_log(f"⚠️ {platform}: ingen login gemt", "warning")
                continue
            add_log(f"⏰ Scheduler poster på {platform}: {product['title'][:30]}", "info")
            asyncio.create_task(asyncio.to_thread(_pw_post_sync, platform, caption_text, image_url, user, pwd))

        # Gem log + opdater times-tæller
        slog = store.setdefault("scheduler_log", {})
        slog[post_key] = {
            "product_id": product["id"],
            "product": product["title"],
            "time": current_time,
            "date": today
        }
        # Opdater timetæller
        hour_key = now.strftime("%Y-%m-%dT%H")
        slog[f"hourly_{hour_key}"] = slog.get(f"hourly_{hour_key}", 0) + len(active_platforms)
        save_store()

    except Exception as e:
        add_log(f"❌ Scheduler fejl: {str(e)[:80]}", "error")


async def run_scheduler():
    global _scheduler_running
    _scheduler_running = True

    # Startup: ryd "posting" poster fra forrige session for alle brugere
    for uid in get_all_user_ids():
        tokens = set_user_context(uid)
        cleaned = 0
        for post in store.get("scheduled_posts", []):
            if post.get("status") == "posting":
                post["status"] = "scheduled"
                cleaned += 1
        if cleaned:
            save_store()
            add_log(f"🔄 {cleaned} opslag sat tilbage til scheduled ved opstart", "info")
        reset_user_context(tokens)

    while True:
        try:
            await asyncio.sleep(30)
            for uid in get_all_user_ids():
                tokens = set_user_context(uid)
                try:
                    await _run_for_user()
                except Exception:
                    pass
                finally:
                    reset_user_context(tokens)
        except Exception:
            await asyncio.sleep(60)


def _pw_post_sync(platform, content, image_url, username, password):
    import asyncio as _asyncio
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    try:
        from routes.playwright import _pw_post
        loop.run_until_complete(_pw_post(platform, content, image_url, username, password))
    except Exception as e:
        pass
    finally:
        loop.close()


@router.post("/api/scheduler/plan_tomorrow")
async def trigger_plan_tomorrow():
    today = datetime.now().strftime("%Y-%m-%d")
    plan_key = f"plan_{today}"
    if plan_key in store.get("scheduler_log", {}):
        del store["scheduler_log"][plan_key]
    await plan_next_day()
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    planned = [p for p in store.get("scheduled_posts", []) if p.get("scheduled_time","").startswith(tomorrow)]
    return {"status": "ok", "planned": len(planned), "posts": planned}

@router.post("/api/scheduler/plan_week")
async def trigger_plan_week():
    auto = store.get("automation", {})
    if not auto.get("active"):
        return {"status": "ok", "planned": 0, "message": "Automation er slået fra"}
    post_times = auto.get("post_times", ["09:00", "14:00", "18:00"])
    schedule_days = auto.get("schedule_days", [1,2,3,4,5])
    products = store.get("manual_products", []) + store.get("shopify_products_cache", [])
    if not products:
        return {"status": "ok", "planned": 0, "message": "Ingen produkter"}
    store.setdefault("scheduler_log", {})
    store.setdefault("scheduled_posts", [])
    total_planned = 0
    now = datetime.now()
    for day_offset in range(1, 8):
        day = now + timedelta(days=day_offset)
        if day.isoweekday() not in schedule_days:
            continue
        date_str = day.strftime("%Y-%m-%d")
        for t in post_times:
            post_key = f"{date_str}_{t}"
            if post_key in store["scheduler_log"]:
                continue
            try:
                dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M")
            except Exception:
                continue
            prod = random.choice(products)
            imgs = prod.get("images", [])
            if not imgs and prod.get("image"):
                imgs = [prod["image"]]
            plats = auto.get("platforms", {})
            active_plats = [p for p,cfg in plats.items() if isinstance(cfg, dict) and cfg.get("active") and cfg.get("auto_post")]
            platform = active_plats[0] if active_plats else "instagram"
            store["scheduled_posts"].append({
                "id": f"auto_{date_str}_{t}_{random.randint(1000,9999)}",
                "platform": platform,
                "content": f"Tjek vores {prod['title']} 🔥",
                "image_url": imgs[0] if imgs else "",
                "product_id": prod["id"],
                "title": prod["title"],
                "scheduled_time": dt.isoformat(),
                "status": "scheduled",
                "source": "auto_plan"
            })
            store["scheduler_log"][post_key] = True
            total_planned += 1
    save_store()
    return {"status": "ok", "planned": total_planned}

@router.post("/api/scheduler/plan_month")
async def trigger_plan_month():
    auto = store.get("automation", {})
    if not auto.get("active"):
        return {"status": "ok", "planned": 0, "message": "Automation er slået fra"}
    post_times = auto.get("post_times", ["09:00", "14:00", "18:00"])
    schedule_days = auto.get("schedule_days", [1,2,3,4,5])
    products = store.get("manual_products", []) + store.get("shopify_products_cache", [])
    if not products:
        return {"status": "ok", "planned": 0, "message": "Ingen produkter"}
    store.setdefault("scheduler_log", {})
    store.setdefault("scheduled_posts", [])
    total_planned = 0
    now = datetime.now()
    for day_offset in range(1, 31):
        day = now + timedelta(days=day_offset)
        if day.isoweekday() not in schedule_days:
            continue
        date_str = day.strftime("%Y-%m-%d")
        for t in post_times:
            post_key = f"{date_str}_{t}"
            if post_key in store["scheduler_log"]:
                continue
            try:
                dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M")
            except Exception:
                continue
            prod = random.choice(products)
            imgs = prod.get("images", [])
            if not imgs and prod.get("image"):
                imgs = [prod["image"]]
            store["scheduled_posts"].append({
                "id": f"auto_{date_str}_{t}_{random.randint(1000,9999)}",
                "platform": list(auto.get("platforms", {}).keys())[0] if auto.get("platforms") else "instagram",
                "content": f"Tjek vores {prod['title']} 🔥",
                "image_url": imgs[0] if imgs else "",
                "product_id": prod["id"],
                "title": prod["title"],
                "scheduled_time": dt.isoformat(),
                "status": "scheduled",
                "source": "auto_plan"
            })
            store["scheduler_log"][post_key] = True
            total_planned += 1
    save_store()
    return {"status": "ok", "planned": total_planned}

@router.post("/api/scheduler/add_time_slot")
async def add_time_slot(req: Request):
    d = await req.json()
    new_time = d.get("time", "12:00")
    auto = store.get("automation", {})
    if not auto.get("active"):
        return {"status": "ok", "added": 0}
    now = datetime.now()
    scheduled = store.get("scheduled_posts", [])
    future_dates = set()
    for p in scheduled:
        st = p.get("scheduled_time", "")
        if st:
            try:
                dt = datetime.fromisoformat(st)
                if dt > now:
                    future_dates.add(dt.strftime("%Y-%m-%d"))
            except Exception:
                pass
    if not future_dates:
        return {"status": "ok", "added": 0}
    all_prods = store.get("shopify_products_cache", []) + store.get("manual_products", [])
    platforms_cfg = auto.get("platforms", {})
    active_platforms = [p for p, v in platforms_cfg.items() if isinstance(v, dict) and v.get("auto_post")]
    if not all_prods or not active_platforms:
        return {"status": "ok", "added": 0}
    added = 0
    new_posts = []
    for date_str in sorted(future_dates):
        for platform in active_platforms:
            exists = any(p.get("scheduled_time","").startswith(f"{date_str}T{new_time}") and p.get("platform") == platform for p in scheduled)
            if exists:
                continue
            prod = random.choice(all_prods)
            imgs = prod.get("images", [])
            if not imgs and prod.get("image"):
                imgs = [prod["image"]]
            imgs = [i.get("src","") if isinstance(i, dict) else i for i in imgs if i]
            content_list = store.get("product_content", {}).get(str(prod["id"]), [])
            captions = [c for c in content_list if c.get("type") == "caption"]
            caption = captions[0]["content"] if captions else f"Tjek vores {prod['title']}! 🔥"
            new_posts.append({
                "id": f"auto_{date_str}_{new_time}_{platform}",
                "platform": platform, "content": caption,
                "image_url": imgs[0] if imgs else "",
                "scheduled_time": f"{date_str}T{new_time}",
                "product_id": str(prod["id"]),
                "product_title": prod.get("title", ""),
                "status": "scheduled", "mode": "ui", "source": "auto_plan"
            })
            added += 1
    store["scheduled_posts"].extend(new_posts)
    save_store()
    add_log(f"⏰ Ny tid {new_time} tilføjet — {added} opslag planlagt fremadrettet", "success")
    return {"status": "ok", "added": added}

@router.get("/api/scheduler/status")
def scheduler_status():
    auto = store.get("automation", {})
    upcoming = []
    if auto.get("active"):
        post_times = auto.get("post_times", ["09:00", "14:00", "18:00"])
        schedule_days = auto.get("schedule_days", [1,2,3,4,5])
        products = store.get("manual_products", []) + store.get("shopify_products_cache", [])
        auto_groups = auto.get("auto_groups", [])
        if auto_groups:
            filtered = [p for p in products if p.get("group","") in auto_groups]
            if filtered:
                products = filtered
        today = datetime.now()
        scheduler_log = store.get("scheduler_log", {})
        for day_offset in range(7):
            day = today + timedelta(days=day_offset)
            if day.isoweekday() not in schedule_days:
                continue
            date_str = day.strftime("%Y-%m-%d")
            for t in post_times:
                post_key = f"{date_str}_{t}"
                if post_key in scheduler_log:
                    continue
                dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M")
                if dt < today:
                    continue
                prod = random.choice(products) if products else None
                upcoming.append({
                    "scheduled_time": dt.isoformat(),
                    "time": t,
                    "date": date_str,
                    "product": prod["title"] if prod else "Ingen produkter",
                    "product_id": prod["id"] if prod else None,
                    "image": prod.get("image","") if prod else "",
                })
                if len(upcoming) >= 10:
                    break
            if len(upcoming) >= 10:
                break
    return {
        "active": auto.get("active", False),
        "post_times": auto.get("post_times", ["09:00", "14:00", "18:00"]),
        "schedule_days": auto.get("schedule_days", [1,2,3,4,5]),
        "last_posts": store.get("scheduler_log", {}),
        "upcoming": upcoming
    }
