from fastapi import APIRouter, HTTPException, BackgroundTasks, UploadFile, File, Request, Depends
from routes.auth import get_current_user
from typing import Optional, List
import httpx, os, json, asyncio, shutil
from datetime import datetime, timedelta
import random
from services.store import store, save_store, add_log, load_store, get_all_user_ids, set_user_context, reset_user_context
from models import Settings, ApiToggle, PlatformAutomation, ContentRequest, PostRequest, DMReplyRequest, AutomationSettings, DMSettings, ManualProduct, Creator, PlaywrightPost

router = APIRouter()

_scheduler_running = False

async def _generate_autopilot_content(
    niche: str,
    product_title: str = "",
    product_desc: str = "",
    platform: str = "instagram",
    language: str = "english",
) -> dict:
    """Generate AI caption + hashtags. Returns {"caption": str, "hashtags": [str]} or {} on failure."""
    import json as _json
    s = store.get("settings", {})
    if product_title:
        subject = f"a product called '{product_title}'" + (f" — {product_desc[:120]}" if product_desc else "")
    else:
        subject = f"a {niche or 'business'} brand"
    style_map = {
        "instagram": "Instagram caption with emojis, 100-180 chars",
        "tiktok": "TikTok caption with hook and emojis, 100-150 chars",
        "youtube": "YouTube Shorts description, 100-200 chars",
        "twitter": "X/Twitter post, punchy, max 250 chars",
        "facebook": "Facebook post, conversational, 100-200 chars",
    }
    style = style_map.get(platform, "social media caption with emojis, 100-180 chars")
    lang_str = "" if language in ("english", "") else f"Write everything in {language.capitalize()}. "
    prompt = (
        f"{lang_str}Write a {style} for {subject}.\n"
        'Return ONLY valid JSON: {"caption": "<text>", "hashtags": ["tag1","tag2","tag3","tag4","tag5"]}'
    )
    def _parse(text):
        text = text.strip()
        s_idx = text.find("{"); e_idx = text.rfind("}") + 1
        if s_idx >= 0 and e_idx > s_idx:
            try:
                return _json.loads(text[s_idx:e_idx])
            except Exception:
                return {}
        return {}
    anthropic_key = s.get("anthropic_key", "")
    if anthropic_key and "••••" not in anthropic_key:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post("https://api.anthropic.com/v1/messages",
                    headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                    json={"model": "claude-haiku-4-5-20251001", "max_tokens": 400,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=20)
                r.raise_for_status()
                result = _parse(r.json()["content"][0]["text"])
                if result.get("caption"):
                    return result
        except Exception as _e:
            add_log(f"AI content (Anthropic) error: {str(_e)[:60]}", "error")
    openai_key = s.get("openai_key", "")
    if openai_key and "••••" not in openai_key:
        try:
            async with httpx.AsyncClient() as c:
                r = await c.post("https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
                    json={"model": "gpt-4o-mini", "max_tokens": 400,
                          "messages": [{"role": "user", "content": prompt}]},
                    timeout=20)
                r.raise_for_status()
                result = _parse(r.json()["choices"][0]["message"]["content"])
                if result.get("caption"):
                    return result
        except Exception as _e:
            add_log(f"AI content (OpenAI) error: {str(_e)[:60]}", "error")
    return {}


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
        add_log("📅 Tomorrow is not a scheduled post day — skipping", "info")
        return
    post_times = auto.get("post_times", ["09:00", "14:00", "18:00"])
    date_str = tomorrow.strftime("%Y-%m-%d")
    existing = [p for p in store.get("scheduled_posts", []) if p.get("scheduled_time","").startswith(date_str) and p.get("source") == "auto_plan"]
    if existing:
        add_log(f"📅 Plan for {date_str} already exists ({len(existing)} posts) — skipping", "info")
        return
    all_products = await get_all_products_cached()
    if not all_products:
        add_log("⚠️ No products — cannot plan tomorrow's posts", "warning")
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
        add_log("⚠️ No platforms with auto-post enabled", "warning")
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
        caption_text = captions[0]["content"] if captions else f"Check out {product['title']}! 🔥"
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
        add_log(f"✅ Plan ready: {len(new_posts)} posts scheduled for {date_str}", "success")
    else:
        add_log(f"⚠️ No new posts planned for {date_str}", "warning")


async def _run_for_user(force: bool = False):
    """Run scheduler logic for the current user (set via context)."""
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

        # ── AUTO-REPOST at 08:00 ──────────────────────────────────────
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
                            store.get("scheduled_posts", {}).insert(0, {
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
                        add_log(f"♻️ Auto-repost scheduled for {', '.join(platforms or ['instagram'])}", "info")

        # ── DAILY PLANNER at 20:00 ─────────────────────────────────────
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
                    add_log(f"📅 Daily planner: {future_planned} days already scheduled — skipping", "info")
                    store.setdefault("scheduler_log", {})[plan_key] = True
                    save_store()
                else:
                    await plan_next_day()
                    store.setdefault("scheduler_log", {})[plan_key] = True
                    save_store()

        # ── SCHEDULED POSTS WORKER ─────────────────────────────────────
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
            prod_title = post.get("title", "Post")
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
                add_log(f"📅 Executing scheduled post via Instagram API...", "info")
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
                            add_log(f"✅ Scheduled post published via API: {pt[:25]}", "success")
                            now_s = datetime.now()
                            key = f"{now_s.strftime('%Y-%m-%d')}_{now_s.strftime('%H:%M')}_manual"
                            store.setdefault("scheduler_log", {})[key] = {
                                "product": pt, "platform": "instagram",
                                "time": now_s.strftime("%H:%M"), "date": now_s.strftime("%Y-%m-%d")
                            }
                            save_store()
                        else:
                            add_log(f"❌ API post error: {result.get('message','')}", "error")
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
                    add_log(f"⚠️ Scheduled post: no credentials for {platform}", "warning")
                    continue
                user = user or "session"
                pwd = pwd or "session"
            add_log(f"📅 Executing scheduled post on {platform}: {prod_title[:25]}...", "info")
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

        # Quiet hours — no posting between 23:00–07:00 unless disabled
        if not force and auto.get("respect_quiet_hours", True):
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

        # Rate limit: max posts per hour
        max_per_hour = auto.get("max_posts_per_hour", 0)
        if not force and max_per_hour and max_per_hour > 0:
            hour_key = now.strftime("%Y-%m-%dT%H")
            posts_this_hour = store.get("scheduler_log", {}).get(f"hourly_{hour_key}", 0)
            if posts_this_hour >= max_per_hour:
                return

        weekday = now.isoweekday()
        schedule_days = auto.get("schedule_days", [1,2,3,4,5])
        if not force and weekday not in schedule_days:
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

        if not force and not time_match:
            return

        today = now.strftime("%Y-%m-%d")
        last_posts = store.get("scheduler_log", {})
        post_key = f"{today}_{current_time}"
        if not force and post_key in last_posts:
            return

        # Determine active platforms early — bail if none enabled
        platforms_cfg = auto.get("platforms", {})
        if isinstance(platforms_cfg, list):
            active_platforms = platforms_cfg
        else:
            active_platforms = [p for p, v in platforms_cfg.items() if isinstance(v, dict) and v.get("auto_post", False)]
        if not active_platforms:
            add_log("⚠️ Auto Pilot: no platforms have auto-post enabled", "warning")
            return

        niche = auto.get("niche") or store.get("settings", {}).get("niche") or ""

        # ── Product selection ──────────────────────────────────────────
        all_products = await get_all_products_cached()
        auto_groups = auto.get("auto_groups", [])
        if auto_groups and all_products:
            products = [p for p in all_products if p.get("group", "") in auto_groups] or all_products
        else:
            products = all_products

        product = None
        caption_text = ""
        image_url = None

        if products:
            if auto.get("post_order", "random") == "random":
                product = random.choice(products)
            else:
                product = sorted(products, key=lambda p: p.get("created", ""), reverse=True)[0]

            if auto.get("no_duplicate_days", True):
                yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
                yesterday_posts = [v for k, v in last_posts.items()
                                   if isinstance(v, dict) and k.startswith(yesterday)]
                if any(p.get("product_id") == product["id"] for p in yesterday_posts):
                    other_products = [p for p in products if p["id"] != product["id"]]
                    if other_products:
                        product = random.choice(other_products)

            product_images = product.get("images", [])
            if not product_images and product.get("image"):
                product_images = [product["image"]]
            if len(product_images) > 1:
                image_url = product_images
                add_log(f"🖼️ {len(product_images)} images — {product['title'][:20]}", "info")
            elif product_images:
                image_url = product_images[0]

            product_content = (store.get("product_content", {}).get(str(product["id"]), []) or
                                store.get("product_content", {}).get(product["id"], []))
            captions = [c for c in product_content if c.get("type") == "caption"]

            if captions:
                caption_text = captions[0]["content"]
            else:
                for _plat in active_platforms:
                    _lang2 = store.get("settings", {}).get("content_language", "english")
                    ai_result = await _generate_autopilot_content(language=_lang2,
                        niche=niche,
                        product_title=product["title"],
                        product_desc=product.get("description", ""),
                        platform=_plat,
                    )
                    if ai_result.get("caption"):
                        caption_text = ai_result["caption"]
                        tags = ai_result.get("hashtags", [])
                        if tags:
                            caption_text += "\n\n" + " ".join(f"#{t.lstrip('#')}" for t in tags)
                        pc = store.setdefault("product_content", {}).setdefault(str(product["id"]), [])
                        pc.append({"type": "caption", "content": caption_text,
                                   "platform": _plat, "auto_generated": True})
                        save_store()
                        break
                if not caption_text:
                    caption_text = f"Check out {product['title']}! 🔥"

        else:
            if not niche:
                add_log(
                    "⚠️ Auto Pilot: no products and no niche configured — "
                    "connect Shopify, add products, or set your niche in Auto Pilot settings",
                    "warning"
                )
                store.setdefault("scheduler_log", {})[post_key] = {
                    "skipped": True, "reason": "no_products_no_niche", "date": today
                }
                save_store()
                return

            _lang = store.get("settings", {}).get("content_language", "english")
            ai_result = await _generate_autopilot_content(niche=niche, platform=active_platforms[0], language=_lang)
            if not ai_result.get("caption"):
                add_log(
                    "⚠️ Auto Pilot: no products and AI content generation failed — "
                    "add an OpenAI or Anthropic API key in Settings",
                    "warning"
                )
                store.setdefault("scheduler_log", {})[post_key] = {
                    "skipped": True, "reason": "no_products_no_ai", "date": today
                }
                save_store()
                return

            caption_text = ai_result["caption"]
            tags = ai_result.get("hashtags", [])
            if tags:
                caption_text += "\n\n" + " ".join(f"#{t.lstrip('#')}" for t in tags)

        # ── Post to each active platform ─────────────────────────────
        settings = store.get("settings", {})
        posted_count = 0
        prod_label = product["title"] if product else niche

        for platform in active_platforms:
            if platform == "instagram" and settings.get("instagram_api_connected"):
                add_log(f"⏰ Auto Pilot → Instagram: {prod_label[:30]}", "info")
                async def _auto_ig(pc=caption_text, iu=image_url, sd=settings, lbl=prod_label):
                    try:
                        from instagram_api import post_to_instagram, refresh_token_if_needed
                        token = sd.get("instagram_api_token", "")
                        ig_id = sd.get("instagram_ig_id", "")
                        exp   = sd.get("instagram_api_expires")
                        token, _ = await refresh_token_if_needed(token, exp)
                        img_url  = iu if isinstance(iu, str) else None
                        img_urls = iu if isinstance(iu, list) else None
                        result = await post_to_instagram(ig_id, token, pc, image_url=img_url, image_urls=img_urls)
                        if result.get("status") == "published":
                            add_log(f"✅ Posted to Instagram: {pc[:60]}…", "success")
                        else:
                            add_log(f"❌ Instagram error: {result.get('message','unknown')[:60]}", "error")
                    except Exception as _e:
                        add_log(f"❌ Instagram exception: {str(_e)[:80]}", "error")
                asyncio.create_task(_auto_ig())
                posted_count += 1
                continue

            if platform == "tiktok" and settings.get("tiktok_api_connected"):
                add_log(f"⏰ Auto Pilot → TikTok: {prod_label[:30]}", "info")
                async def _auto_tt(pc=caption_text, iu=image_url, sd=settings, lbl=prod_label):
                    try:
                        from tiktok_api import refresh_token_if_needed as tt_refresh, publish_to_tiktok
                        token   = sd.get("tiktok_access_token", "")
                        refresh = sd.get("tiktok_refresh_token", "")
                        exp     = sd.get("tiktok_expires_at")
                        new_token, new_ref, new_exp = await tt_refresh(token, refresh, exp)
                        if new_exp:
                            store.get("settings", {})["tiktok_access_token"] = new_token
                            save_store()
                            token = new_token
                        vid_url = iu if isinstance(iu, str) and iu.endswith(('.mp4', '.mov')) else None
                        img_url = iu if isinstance(iu, str) and not vid_url else None
                        result = await publish_to_tiktok(token, pc, video_url=vid_url, image_url=img_url)
                        status = result.get("status", "")
                        if status in ("published", "processing"):
                            add_log(f"✅ Posted to TikTok: {pc[:60]}…", "success")
                        else:
                            add_log(f"❌ TikTok error: {result.get('message','unknown')[:60]}", "error")
                    except Exception as _e:
                        add_log(f"❌ TikTok exception: {str(_e)[:80]}", "error")
                asyncio.create_task(_auto_tt())
                posted_count += 1
                continue

            if platform == "twitter" and settings.get("twitter_api_connected"):
                add_log(f"⏰ Auto Pilot → Twitter/X: {prod_label[:30]}", "info")
                async def _auto_tw(pc=caption_text, sd=settings):
                    try:
                        from twitter_api import post_tweet
                        result = await post_tweet(
                            text=pc,
                            access_token=sd.get("twitter_access_token", ""),
                            access_secret=sd.get("twitter_access_secret", ""),
                            consumer_key=sd.get("twitter_api_key", ""),
                            consumer_secret=sd.get("twitter_api_secret", ""),
                        )
                        if result.get("ok") or result.get("id"):
                            add_log(f"✅ Posted to Twitter/X: {pc[:60]}…", "success")
                        else:
                            add_log(f"❌ Twitter/X error: {str(result)[:60]}", "error")
                    except ImportError:
                        add_log("⏭️ Skipped Twitter/X: twitter_api module not configured", "info")
                    except Exception as _e:
                        add_log(f"❌ Twitter/X exception: {str(_e)[:80]}", "error")
                asyncio.create_task(_auto_tw())
                posted_count += 1
                continue

            if platform == "youtube":
                vid = image_url if isinstance(image_url, str) else ""
                if vid and vid.endswith(('.mp4', '.mov', '.avi')):
                    add_log("⏰ Auto Pilot → YouTube: video queued", "info")
                    posted_count += 1
                else:
                    add_log("⏭️ Skipped YouTube: video content required for YouTube posts", "info")
                continue

            # Playwright fallback for other platforms
            user = settings.get(f"{platform}_user", "")
            pwd  = settings.get(f"{platform}_pass", "")
            if not user or not pwd:
                add_log(f"⏭️ Skipped {platform}: no account connected", "info")
                continue
            add_log(f"⏰ Auto Pilot → {platform}: {prod_label[:30]}", "info")
            asyncio.create_task(asyncio.to_thread(
                _pw_post_sync, platform, caption_text, image_url or "", user, pwd
            ))
            posted_count += 1

        # Save log + update hourly counter
        slog = store.setdefault("scheduler_log", {})
        slog[post_key] = {
            "product_id": product["id"] if product else None,
            "product": product["title"] if product else f"AI:{niche}",
            "time": current_time,
            "date": today,
        }
        hour_key2 = now.strftime("%Y-%m-%dT%H")
        slog[f"hourly_{hour_key2}"] = slog.get(f"hourly_{hour_key2}", 0) + max(posted_count, 1)
        save_store()
        if posted_count > 0:
            try:
                from services.store import _uid_ctx
                _uid = _uid_ctx.get(None)
                if _uid:
                    from routes.notifications import push_notification
                    _prod_lbl = product["title"][:40] if product else f"AI content ({niche})" if niche else "AI content"
                    push_notification(_uid, "autopilot_posted", "Auto Pilot posted",
                                      f"Posted to {posted_count} platform(s): {_prod_lbl}")
            except Exception:
                pass

            # Send CONTENT_POSTED event to NIE (fire-and-forget)
            try:
                import asyncio as _asyncio, httpx as _httpx
                from services.store import _uid_ctx as _uctx2
                _uid2 = _uctx2.get(None)
                import hashlib as _hl
                _hash = _hl.sha256((_uid2 or "").encode()).hexdigest()[:16] if _uid2 else ""
                _niche2 = store.get("automation", {}).get("niche", "")
                _plat2  = active_platforms[0] if active_platforms else "unknown"
                _nie_data = {
                    "content_type":   "image",
                    "platform":       _plat2,
                    "niche":          _niche2,
                    "hour":           datetime.now().hour,
                    "hashtag_categories": [_niche2] if _niche2 else [],
                    "source":         "autopilot",
                }
                async def _send_nie():
                    try:
                        async with _httpx.AsyncClient(timeout=4) as _c:
                            await _c.post(
                                "http://localhost:4000/api/nie/event",
                                json={"platform": _plat2, "event_type": "CONTENT_POSTED",
                                      "niche": _niche2, "data": _nie_data, "user_id_hash": _hash},
                            )
                    except Exception:
                        pass
                _asyncio.create_task(_send_nie())
            except Exception:
                pass

    except Exception as e:
        add_log(f"❌ Scheduler error: {str(e)[:80]}", "error")

async def send_daily_reminders():
    """Send re-engagement emails to users who haven't logged in for 3+ days."""
    from services.store import get_all_user_ids, _load_user_store
    from services.users import get_user_by_id
    from routes.email import send_reminder_email
    from datetime import datetime, timezone
    import logging

    now = datetime.utcnow()
    sent = 0
    for uid in get_all_user_ids():
        try:
            user = get_user_by_id(uid)
            if not user or not user.get("is_active"):
                continue
            # Check notification preference
            ustore = _load_user_store(uid)
            if ustore.get("notif_disabled"):
                continue
            last_login_str = user.get("last_login", user.get("created_at", ""))
            if not last_login_str:
                continue
            try:
                last_login = datetime.fromisoformat(last_login_str.replace("Z", ""))
            except Exception:
                continue
            days_away = (now - last_login).days
            if days_away < 3:
                continue
            # Avoid sending more than once per day
            reminder_key = f"reminder_{now.strftime('%Y-%m-%d')}"
            if ustore.get(reminder_key):
                continue
            ok = send_reminder_email(user["email"], user.get("name", ""), days_away)
            if ok:
                ustore[reminder_key] = True
                from services.store import _save_user_store
                _save_user_store(uid, ustore)
                sent += 1
        except Exception:
            pass
    if sent:
        print(f"[reminder] Sent {sent} reminder emails")



async def check_auto_topup():
    """Check all users for auto top-up triggers and process if needed."""
    from services.users import load_users
    from services.store import _load_user_store, _save_user_store
    from routes.billing import PLANS
    from routes.email import send_system_email
    users = load_users().get("users", [])
    for user in users:
        uid = user["id"]
        ustore = _load_user_store(uid)
        billing = ustore.get("billing", {})
        if not billing.get("auto_topup_enabled"):
            continue
        threshold = billing.get("auto_topup_threshold", 50)
        amount = billing.get("auto_topup_amount", 100)
        current_credits = billing.get("credits", 0)
        if current_credits > threshold:
            continue
        # Check cooldown — don't top up more than once per 24h
        from datetime import datetime, timedelta
        last = billing.get("auto_topup_last")
        if last:
            try:
                if (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds() < 86400:
                    continue
            except Exception:
                pass
        # Add credits (in production this would charge the saved payment method via Stripe)
        billing["credits"] = current_credits + amount
        billing["auto_topup_last"] = datetime.utcnow().isoformat()
        _save_user_store(uid, ustore)
        # Notify user by email
        try:
            send_system_email(
                user["email"],
                f"Auto Top-Up: {amount} credits added",
                f"<p>Hi {user.get('name','there')},</p><p>Your credits were running low ({current_credits} remaining). {amount} credits have been added automatically.</p><p>New balance: {billing['credits']} credits.</p><p>Go to <a href='https://ugoingviral.com/app'>your dashboard</a> to manage auto top-up settings.</p>"
            )
        except Exception:
            pass

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
            add_log(f"🔄 Reset {cleaned} stuck posts to scheduled at startup", "info")
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
            pass
        # Daily reminder at 09:00 CET (08:00 UTC)
        _now_utc = __import__('datetime').datetime.utcnow()
        if _now_utc.hour == 8 and _now_utc.minute < 2:
            try:
                await send_daily_reminders()
                await check_auto_topup()
            except Exception as _re:
                print(f"[reminder] error: {_re}")
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
        del store.get("scheduler_log", {})[plan_key]
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
        return {"status": "ok", "planned": 0, "message": "No products"}
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
            if post_key in store.get("scheduler_log", {}):
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
            store.get("scheduled_posts", {}).append({
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
            store.get("scheduler_log", {})[post_key] = True
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
        return {"status": "ok", "planned": 0, "message": "No products"}
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
            if post_key in store.get("scheduler_log", {}):
                continue
            try:
                dt = datetime.strptime(f"{date_str} {t}", "%Y-%m-%d %H:%M")
            except Exception:
                continue
            prod = random.choice(products)
            imgs = prod.get("images", [])
            if not imgs and prod.get("image"):
                imgs = [prod["image"]]
            store.get("scheduled_posts", {}).append({
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
            store.get("scheduler_log", {})[post_key] = True
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
    store.get("scheduled_posts", {}).extend(new_posts)
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



# == DAILY CONTENT REFRESH (06:00 CET = 05:00 UTC) ==

# == DAILY CONTENT REFRESH (06:00 CET = 05:00 UTC) ==
async def run_content_refresh():
    import asyncio as _aio, httpx as _httpx, os as _os
    from datetime import datetime as _dt
    from services.store import get_all_user_ids as _uids, _load_user_store as _lus, _save_user_store as _sus
    from services.users import load_users as _lu
    from routes.agent import _get_content_insights
    BOT_TOKEN = _os.getenv('TELEGRAM_BOT_TOKEN', '')
    async def _tg(chat_id, text):
        if not BOT_TOKEN or not chat_id: return
        try:
            async with _httpx.AsyncClient(timeout=8) as c:
                await c.post('https://api.telegram.org/bot' + BOT_TOKEN + '/sendMessage',
                             json={'chat_id': chat_id, 'text': text, 'parse_mode': 'Markdown'})
        except Exception: pass
    async def _ai_suggestion(insights, api_keys, niche):
        best_plat = insights.get('best_platform') or 'instagram'
        best_ct   = insights.get('best_content_type') or 'caption'
        best_hour = insights.get('best_posting_hour')
        time_hint = ' Schedule for {:02d}:00.'.format(best_hour) if best_hour is not None else ''
        prompt = 'Suggest ONE content idea for {}. Type: {}. Niche: {}. 2 sentences max.{}'.format(
            best_plat, best_ct, niche or 'general', time_hint)
        ak = api_keys.get('anthropic_key', '') or _os.getenv('ANTHROPIC_API_KEY', '')
        if ak and '\u2022\u2022' not in ak:
            try:
                async with _httpx.AsyncClient(timeout=15) as c:
                    r = await c.post('https://api.anthropic.com/v1/messages',
                        headers={'x-api-key': ak, 'anthropic-version': '2023-06-01',
                                 'content-type': 'application/json'},
                        json={'model': 'claude-haiku-4-5-20251001', 'max_tokens': 120,
                              'messages': [{'role': 'user', 'content': prompt}]})
                    r.raise_for_status()
                    return r.json()['content'][0]['text'].strip()
            except Exception: pass
        ok = api_keys.get('openai_key', '') or _os.getenv('OPENAI_API_KEY', '')
        if ok and '\u2022\u2022' not in ok:
            try:
                async with _httpx.AsyncClient(timeout=15) as c:
                    r = await c.post('https://api.openai.com/v1/chat/completions',
                        headers={'Authorization': 'Bearer ' + ok, 'content-type': 'application/json'},
                        json={'model': 'gpt-4o-mini', 'max_tokens': 100,
                              'messages': [{'role': 'user', 'content': prompt}]})
                    r.raise_for_status()
                    return r.json()['choices'][0]['message']['content'].strip()
            except Exception: pass
        return 'Try a {} on {} - your audience responds best to this.'.format(best_ct, best_plat)
    _fired_today = ''
    while True:
        await _aio.sleep(60)
        now_utc = _dt.utcnow()
        today   = now_utc.strftime('%Y-%m-%d')
        if not (now_utc.hour == 5 and now_utc.minute < 2): continue
        if _fired_today == today: continue
        _fired_today = today
        try:
            users_data = _lu()
            uid_list   = _uids()
        except Exception: continue
        users_by_id = {u['id']: u for u in users_data.get('users', [])}
        for uid in uid_list:
            try:
                ustore   = _lus(uid)
                auto_cfg = ustore.get('automation', {})
                if not auto_cfg.get('active', False) or not ustore.get('content_performance'): continue
                history = ustore.get('history', [])
                last_ts = None
                for h in history:
                    ts_str = h.get('timestamp', h.get('created_at', ''))
                    try:
                        ts = _dt.fromisoformat(ts_str[:19])
                        if last_ts is None or ts > last_ts: last_ts = ts
                    except Exception: pass
                if last_ts and (_dt.utcnow() - last_ts).days < 3: continue
                insights = _get_content_insights(uid)
                if not insights: continue
                settings = ustore.get('settings', {})
                api_keys = {'anthropic_key': settings.get('anthropic_key', ''),
                            'openai_key':    settings.get('openai_key', '')}
                niche = auto_cfg.get('niche', '')
                suggestion_text = await _ai_suggestion(insights, api_keys, niche)
                suggestions = ustore.setdefault('pending_suggestions', [])
                suggestions.append({
                    'id':           'sug_' + today + '_' + uid[:8],
                    'created_at':   _dt.utcnow().isoformat(),
                    'platform':     insights.get('best_platform', 'instagram'),
                    'content_type': insights.get('best_content_type', 'caption'),
                    'suggestion':   suggestion_text,
                    'best_hour':    insights.get('best_posting_hour'),
                    'source':       'auto_refresh',
                    'seen':         False,
                })
                ustore['pending_suggestions'] = suggestions[-20:]
                _sus(uid, ustore)
                user_rec = users_by_id.get(uid, {})
                tg_chat  = user_rec.get('telegram_id') or ustore.get('telegram_chat_id')
                if tg_chat:
                    best_plat = insights.get('best_platform', 'your platform')
                    msg = ('\U0001F4CA *UgoingViral \u2014 New Content Idea Ready*\n\n'
                           'Based on your best performing content on ' + best_plat + ', I have a new idea ready.\n\n'
                           '\U0001F4A1 _' + suggestion_text + '_\n\n'
                           'Check your dashboard to review and schedule it! \U0001F680')
                    await _tg(tg_chat, msg)
            except Exception: continue


@router.get('/api/content/suggestions')
def get_pending_suggestions(current_user: dict = Depends(get_current_user)):
    from services.store import _load_user_store, _save_user_store
    ustore      = _load_user_store(current_user['id'])
    suggestions = ustore.get('pending_suggestions', [])
    changed     = False
    for s in suggestions:
        if not s.get('seen'):
            s['seen'] = True
            changed   = True
    if changed: _save_user_store(current_user['id'], ustore)
    return {'suggestions': list(reversed(suggestions))}
