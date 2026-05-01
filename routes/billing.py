import os
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store

router = APIRouter()

PLANS = {
    "free": {
        "name": "Free", "price": 0, "credits": 10,
        "sub_accounts": 0, "video": False, "live_support": False,
        "features": ["10 videos lifetime", "1 platform", "AI text content", "Content history"],
    },
    "starter": {
        "name": "Starter", "price": 40, "credits": 200,
        "sub_accounts": 0, "video": False, "live_support": False,
        "features": ["Everything in Free +", "200 credits/mo", "All platforms", "Auto-post & scheduling", "Content Pipeline (text)", "1 main account"],
    },
    "growth": {
        "name": "Growth", "price": 57, "credits": 500,
        "sub_accounts": 1, "video": False, "live_support": False,
        "features": ["Everything in Starter +", "500 credits/mo", "1 growth account", "Auto DM replies", "Auto comment replies"],
    },
    "basic": {
        "name": "Basic", "price": 70, "credits": 1000,
        "sub_accounts": 2, "video": False, "live_support": False,
        "features": ["Everything in Growth +", "1,000 credits/mo", "2 sub-accounts", "Media library", "Content Mapper", "Basic analytics"],
    },
    "pro": {
        "name": "Pro", "price": 140, "credits": 2000,
        "sub_accounts": 5, "video": True, "live_support": True,
        "features": ["Everything in Basic +", "2,000 credits/mo", "5 sub-accounts", "AI video generation", "AI voiceover", "Auto DM replies", "Live chat support"],
    },
    "elite": {
        "name": "Elite", "price": 210, "credits": 5000,
        "sub_accounts": 10, "video": True, "live_support": True,
        "features": ["Everything in Pro +", "5,000 credits/mo", "10 sub-accounts", "Priority video rendering", "Growth automation", "Anti-ban pro system"],
    },
    "personal": {
        "name": "Personal", "price": 350, "credits": 999999,
        "sub_accounts": 999, "video": True, "live_support": True,
        "features": ["Everything in Elite +", "Unlimited credits/mo", "Unlimited sub-accounts", "Dedicated AI assistant", "Priority support", "White-label option"],
    },
    "agency": {
        "name": "Agency", "price": 199, "credits": 2000,
        "sub_accounts": 10, "video": True, "live_support": True,
        "features": ["Everything in Pro +", "2,000 credits/mo", "10 client accounts", "Agency dashboard", "Per-client content settings", "Priority support"],
    },
}

BONUS_CREDITS = {
    "connect_social":  10,
    "first_video":     20,
    "first_post":      15,
    "daily_login":    100,
    "invite_friend":   50,
}

BASE_URL = "https://ugoingviral.com"

# Topup er dyrere per credit end plan-abonnement — incentiverer planopgradering
TOPUP_PACKAGES = {
    100:  {"price": 29,  "name": "100 Credits",  "note": "Quick top-up"},
    500:  {"price": 99,  "name": "500 Credits",  "note": "Popular choice"},
    1500: {"price": 249, "name": "1500 Credits", "note": "Best price per credit"},
}

# Studio video pakker — engangskøb med credits til specifikke workflows
STUDIO_PACKAGES = {
    "serie_3": {
        "name": "3-Scene Serie",
        "price": 149,
        "credits": 450,
        "icon": "🎬",
        "color": "#00e5ff",
        "description": "Perfekt til en kort produkt- eller brand-serie",
        "features": [
            "450 credits inkluderet (0,33 kr/cr)",
            "Op til 3 scener per serie",
            "Smart Clip Cutter — 6 klip",
            "2 output formater (9:16 + 16:9)",
            "ElevenLabs voice-over inkluderet",
            "AI prompt-optimering via Claude",
            "Auto-upload til valgt platform",
        ],
        "scenes": 3, "clip_count": 6, "formats": 2,
    },
    "serie_6": {
        "name": "6-Scene Serie",
        "price": 299,
        "credits": 950,
        "icon": "🎞️",
        "color": "#7c3aed",
        "description": "Fortæl en hel historie — ideel til kampagner",
        "features": [
            "950 credits inkluderet (0,32 kr/cr)",
            "Op til 6 scener per serie",
            "Smart Clip Cutter — op til 10 klip",
            "Alle 4 formater (9:16, 16:9, 1:1, 4:5)",
            "ElevenLabs voice-over inkluderet",
            "AI prompt-optimering + stil-konsistens",
            "Produkt-blend i alle scener",
            "Auto-upload til alle platforme",
        ],
        "scenes": 6, "clip_count": 10, "formats": 4,
    },
    "serie_10": {
        "name": "10-Scene Studio",
        "price": 549,
        "credits": 1800,
        "icon": "🏆",
        "color": "#f59e0b",
        "description": "Professionel mini-serie — brandsæt eller content kampagne",
        "features": [
            "1.800 credits inkluderet (0,31 kr/cr)",
            "Op til 10 scener per serie",
            "Smart Clip Cutter — op til 12 klip",
            "Alle 4 formater inkl. 4:5",
            "ElevenLabs voice-over inkluderet",
            "AI prompt-optimering (Claude Sonnet)",
            "Produkt-blend i alle scener",
            "Multi-provider: Runway + Luma + Replicate",
            "Prioriteret rendering",
            "Auto-upload til alle platforme",
        ],
        "scenes": 10, "clip_count": 12, "formats": 4,
    },
}


# Tilkøb — ekstra under-konto slots (engangskøb, permanent)
ADDON_PACKAGES = {
    "extra_profile_starter": {"name": "Extra Profile (Starter/Growth)", "price": 7,  "sub_slots": 1, "icon": "👥", "description": "Add 1 extra profile — $7/mo (Starter & Growth plans)", "monthly": True},
    "extra_profile_pro":     {"name": "Extra Profile (Basic/Pro/Elite)", "price": 14, "sub_slots": 1, "icon": "👥", "description": "Add 1 extra profile — $14/mo (Basic, Pro & Elite plans)", "monthly": True},
}


class UseCreditsRequest(BaseModel):
    amount: int
    reason: str = ""


@router.get("/api/billing/studio_packages")
def get_studio_packages(current_user: dict = Depends(get_current_user)):
    return {"packages": STUDIO_PACKAGES}


@router.post("/api/billing/create_studio_checkout")
async def create_studio_checkout(body: dict, current_user: dict = Depends(get_current_user)):
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe ikke konfigureret")

    pkg_key = body.get("package", "")
    if pkg_key not in STUDIO_PACKAGES:
        raise HTTPException(status_code=400, detail="Ukendt studio-pakke")

    pkg = STUDIO_PACKAGES[pkg_key]
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "dkk",
                "product_data": {
                    "name": f"UgoingViral {pkg['name']}",
                    "description": pkg["description"],
                },
                "unit_amount": pkg["price"] * 100,
            },
            "quantity": 1,
        }],
        client_reference_id=current_user["id"],
        customer_email=current_user["email"],
        success_url=f"{BASE_URL}/app?payment=success&credits={pkg['credits']}&pkg={pkg_key}&studio=1",
        cancel_url=f"{BASE_URL}/app?payment=cancelled",
        metadata={"type": "studio_package", "package": pkg_key,
                  "credits": str(pkg["credits"]), "user_id": current_user["id"]},
        locale="da",
    )
    return {"url": session.url}


@router.get("/api/billing/topup_packages")
def get_topup_packages(current_user: dict = Depends(get_current_user)):
    return {"packages": TOPUP_PACKAGES}


@router.post("/api/billing/create_custom_topup")
async def create_custom_topup(body: dict, current_user: dict = Depends(get_current_user)):
    """Custom credit top-up — user specifies amount in USD."""
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    amount_usd = int(body.get("amount_usd", body.get("amount_dkk", 0)))
    if amount_usd < 5:
        raise HTTPException(status_code=400, detail="Minimum $5")
    if amount_usd > 1000:
        raise HTTPException(status_code=400, detail="Maximum $1000")

    # Rate: 3 cr/USD — slightly above fixed packages to incentivize package purchases
    credits = int(amount_usd * 3)
    uid = current_user["id"]

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": f"UgoingViral {credits} Credits",
                    "description": f"One-time purchase of {credits} credits (${amount_usd})",
                },
                "unit_amount": amount_usd * 100,
            },
            "quantity": 1,
        }],
        client_reference_id=uid,
        customer_email=current_user["email"],
        success_url=f"{BASE_URL}/app?payment=success&credits={credits}&custom=1",
        cancel_url=f"{BASE_URL}/app?payment=cancelled",
        metadata={"type": "custom_topup", "credits": str(credits), "user_id": uid},
    )
    return {"url": session.url, "credits": credits}


@router.get("/api/billing/addons")
def get_addons(current_user: dict = Depends(get_current_user)):
    """Hent tilkøbspakker og brugerens nuværende tilkøb."""
    billing = store.get("billing", {})
    return {
        "packages": ADDON_PACKAGES,
        "extra_sub_slots": billing.get("extra_sub_slots", 0),
        "subs_blocked": billing.get("subs_blocked", False),
    }


@router.post("/api/billing/create_addon_checkout")
async def create_addon_checkout(body: dict, current_user: dict = Depends(get_current_user)):
    """Stripe checkout for tilkøbspakker (ekstra under-konto slots)."""
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe ikke konfigureret")

    pkg_key = body.get("package", "")
    if pkg_key not in ADDON_PACKAGES:
        raise HTTPException(status_code=400, detail="Ukendt tilkøbspakke")

    pkg = ADDON_PACKAGES[pkg_key]
    uid = current_user["id"]

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "dkk",
                "product_data": {
                    "name": f"UgoingViral {pkg['name']}",
                    "description": pkg["description"],
                },
                "unit_amount": pkg["price"] * 100,
            },
            "quantity": 1,
        }],
        client_reference_id=uid,
        customer_email=current_user["email"],
        success_url=f"{BASE_URL}/app?payment=success&addon={pkg_key}&sub_slots={pkg['sub_slots']}",
        cancel_url=f"{BASE_URL}/app?payment=cancelled",
        metadata={"type": "addon", "addon": pkg_key, "sub_slots": str(pkg["sub_slots"]), "user_id": uid},
        locale="da",
    )
    return {"url": session.url}


@router.get("/api/billing/plan")
def get_plan(current_user: dict = Depends(get_current_user)):
    billing = store.get("billing", {})
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    return {
        "plan": plan_key,
        "plan_name": plan_info["name"],
        "price": plan_info["price"],
        "credits": billing.get("credits", plan_info["credits"]),
        "max_credits": plan_info["credits"],
        "plans": PLANS,
    }


@router.get("/api/billing/usage")
def get_usage(current_user: dict = Depends(get_current_user)):
    billing = store.get("billing", {})
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    credits_left = billing.get("credits", plan_info["credits"])
    usage_log = store.get("api_usage", [])
    return {
        "credits_left": credits_left,
        "max_credits":  plan_info["credits"],
        "plan":         plan_key,
        "log":          usage_log[-20:],
        "credit_costs": {
            "generate_pipeline": 5,
            "generate_video":    20,
            "generate_video_15": 40,
            "generate_video_30": 80,
            "generate_voice":    15,
            "assemble_video":    2,
            "add_captions":      2,
            "auto_post":         1,
        },
    }


@router.post("/api/billing/use_credits")
def use_credits(req: UseCreditsRequest, current_user: dict = Depends(get_current_user)):
    if req.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount skal være positiv")
    billing = store.setdefault("billing", {})
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    current = billing.get("credits", plan_info["credits"])
    if current < req.amount:
        raise HTTPException(status_code=402, detail="Ikke nok credits")
    billing["credits"] = current - req.amount
    store["billing"] = billing
    save_store()
    return {"credits": billing["credits"], "used": req.amount}


def _try_auto_bonus(bonus_key: str) -> int:
    """Award bonus silently. Returns credits awarded (0 if already claimed)."""
    import datetime as _dt
    if bonus_key not in BONUS_CREDITS:
        return 0
    billing = store.setdefault("billing", {})
    claimed = billing.setdefault("claimed_bonuses", [])
    if bonus_key == "daily_login":
        today = _dt.date.today().isoformat()
        if billing.get("last_daily_bonus") == today:
            return 0
        billing["last_daily_bonus"] = today
    elif bonus_key in claimed:
        return 0
    else:
        claimed.append(bonus_key)
    amount = BONUS_CREDITS[bonus_key]
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    billing["credits"] = billing.get("credits", plan_info["credits"]) + amount
    billing["claimed_bonuses"] = claimed
    store["billing"] = billing
    save_store()
    return amount


@router.post("/api/billing/claim_bonus")
def claim_bonus(body: dict, current_user: dict = Depends(get_current_user)):
    bonus_key = body.get("bonus", "")
    if bonus_key not in BONUS_CREDITS:
        raise HTTPException(status_code=400, detail="Ukendt bonus")
    billing = store.setdefault("billing", {})
    claimed = billing.setdefault("claimed_bonuses", [])
    if bonus_key == "daily_login":
        import datetime
        today = datetime.date.today().isoformat()
        last = billing.get("last_daily_bonus", "")
        if last == today:
            raise HTTPException(status_code=409, detail="Daglig bonus allerede hentet i dag")
        billing["last_daily_bonus"] = today
    elif bonus_key in claimed:
        raise HTTPException(status_code=409, detail="Bonus allerede hentet")
    else:
        claimed.append(bonus_key)
    amount = BONUS_CREDITS[bonus_key]
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    billing["credits"] = billing.get("credits", plan_info["credits"]) + amount
    billing["claimed_bonuses"] = claimed
    store["billing"] = billing
    save_store()
    return {"credits": billing["credits"], "bonus": bonus_key, "amount": amount}


@router.post("/api/billing/set_plan")
def set_plan(body: dict, current_user: dict = Depends(get_current_user)):
    plan_key = body.get("plan", "starter")
    if plan_key not in PLANS:
        raise HTTPException(status_code=400, detail="Ukendt plan")
    plan_info = PLANS[plan_key]
    billing = store.setdefault("billing", {})
    billing["plan"] = plan_key
    billing["credits"] = plan_info["credits"]
    store["billing"] = billing
    save_store()
    return {"plan": plan_key, "credits": plan_info["credits"]}


@router.post("/api/billing/create_paypal_checkout")
async def create_paypal_checkout(body: dict, current_user: dict = Depends(get_current_user)):
    """PayPal checkout — plan subscription or one-time credits topup."""
    paypal_client_id     = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    if not paypal_client_id or not paypal_client_secret:
        raise HTTPException(status_code=503, detail="PayPal ikke konfigureret — tilføj PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET i .env")

    import httpx as _httpx
    # Hent access token
    async with _httpx.AsyncClient() as c:
        token_r = await c.post(
            "https://api-m.paypal.com/v1/oauth2/token",
            headers={"Accept": "application/json"},
            auth=(paypal_client_id, paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        if token_r.status_code != 200:
            raise HTTPException(status_code=503, detail="PayPal token fejl")
        access_token = token_r.json()["access_token"]

    payment_type = body.get("type", "plan")
    uid = current_user["id"]

    if payment_type == "credits":
        credits = int(body.get("credits", 0))
        if credits not in TOPUP_PACKAGES:
            raise HTTPException(status_code=400, detail="Ugyldigt credits-pakke")
        pkg = TOPUP_PACKAGES[credits]
        amount_dkk = str(pkg["price"])
        description = f"UgoingViral {pkg['name']} — {credits} credits"
        custom_id = f"credits_{credits}_{uid}"
    else:
        plan_key = body.get("plan", "")
        if plan_key not in PLANS or PLANS[plan_key]["price"] == 0:
            raise HTTPException(status_code=400, detail="Ugyldig plan")
        plan_info = PLANS[plan_key]
        amount_dkk = str(plan_info["price"])
        description = f"UgoingViral {plan_info['name']} — {plan_info['credits']} credits/md"
        custom_id = f"plan_{plan_key}_{uid}"

    async with _httpx.AsyncClient() as c:
        order_r = await c.post(
            "https://api-m.paypal.com/v2/checkout/orders",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={
                "intent": "CAPTURE",
                "purchase_units": [{
                    "description": description,
                    "custom_id": custom_id,
                    "amount": {"currency_code": "DKK", "value": amount_dkk},
                }],
                "application_context": {
                    "brand_name": "UgoingViral",
                    "locale": "da-DK",
                    "return_url": f"{BASE_URL}/app?payment=success&provider=paypal&type={payment_type}&ref={custom_id}",
                    "cancel_url": f"{BASE_URL}/app?payment=cancelled",
                },
            },
        )
        if order_r.status_code not in (200, 201):
            raise HTTPException(status_code=503, detail=f"PayPal ordre fejl: {order_r.text[:200]}")
        order = order_r.json()

    approve_url = next((l["href"] for l in order.get("links", []) if l["rel"] == "approve"), None)
    if not approve_url:
        raise HTTPException(status_code=503, detail="PayPal returnerede ingen godkendelses-URL")
    return {"url": approve_url, "order_id": order["id"]}


@router.post("/api/billing/paypal_capture")
async def paypal_capture(body: dict, current_user: dict = Depends(get_current_user)):
    """Capture PayPal payment after user approves — applies credits/plan."""
    paypal_client_id     = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    order_id = body.get("order_id", "")
    custom_id = body.get("custom_id", "")
    if not order_id:
        raise HTTPException(status_code=400, detail="Mangler order_id")

    import httpx as _httpx
    async with _httpx.AsyncClient() as c:
        token_r = await c.post(
            "https://api-m.paypal.com/v1/oauth2/token",
            headers={"Accept": "application/json"},
            auth=(paypal_client_id, paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        access_token = token_r.json()["access_token"]
        cap_r = await c.post(
            f"https://api-m.paypal.com/v2/checkout/orders/{order_id}/capture",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={},
        )
        if cap_r.status_code not in (200, 201):
            raise HTTPException(status_code=400, detail="PayPal capture fejl")
        cap_data = cap_r.json()

    status = cap_data.get("status", "")
    if status != "COMPLETED":
        raise HTTPException(status_code=400, detail=f"Betaling ikke gennemført: {status}")

    # Parse custom_id: "credits_350_userid" or "plan_pro_userid"
    uid = current_user["id"]
    parts = (custom_id or "").split("_")
    user_store = _load_user_store(uid)
    billing = user_store.setdefault("billing", {})

    if parts[0] == "credits" and len(parts) >= 2:
        credits = int(parts[1])
        plan_key = billing.get("plan", "free")
        plan_info = PLANS.get(plan_key, PLANS["free"])
        billing["credits"] = billing.get("credits", plan_info["credits"]) + credits
        user_store["billing"] = billing
        _save_user_store(uid, user_store)
        return {"status": "ok", "credits_added": credits, "credits": billing["credits"]}

    elif parts[0] == "plan" and len(parts) >= 2:
        plan_key = parts[1]
        if plan_key in PLANS:
            plan_info = PLANS[plan_key]
            billing["plan"] = plan_key
            billing["credits"] = plan_info["credits"]
            user_store["billing"] = billing
            _save_user_store(uid, user_store)
            return {"status": "ok", "plan": plan_key, "credits": plan_info["credits"]}

    raise HTTPException(status_code=400, detail="Ukendt PayPal betaling")


@router.post("/api/billing/create_checkout")
async def create_checkout(body: dict, current_user: dict = Depends(get_current_user)):
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe ikke konfigureret — tilføj STRIPE_SECRET_KEY i .env")

    payment_type = body.get("type", "plan")

    # ── One-time credit top-up ───────────────────────────────────────────────
    if payment_type == "credits":
        credits = int(body.get("credits", 0))
        if credits not in TOPUP_PACKAGES:
            raise HTTPException(status_code=400, detail="Ugyldigt credits-pakke")
        pkg = TOPUP_PACKAGES[credits]
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{
                "price_data": {
                    "currency": "dkk",
                    "product_data": {
                        "name": f"UgoingViral {pkg['name']}",
                        "description": f"Engangskøb af {credits} credits",
                    },
                    "unit_amount": pkg["price"] * 100,
                },
                "quantity": 1,
            }],
            client_reference_id=current_user["id"],
            customer_email=current_user["email"],
            success_url=f"{BASE_URL}/app?payment=success&credits={credits}",
            cancel_url=f"{BASE_URL}/app?payment=cancelled",
            metadata={"type": "credits", "credits": str(credits), "user_id": current_user["id"]},
            locale="da",
        )
        return {"url": session.url}

    # ── Subscription plan ────────────────────────────────────────────────────
    plan_key = body.get("plan", "")
    if plan_key not in PLANS:
        raise HTTPException(status_code=400, detail="Ukendt plan")
    if PLANS[plan_key]["price"] == 0:
        raise HTTPException(status_code=400, detail="Gratis plan kræver ikke betaling")

    plan_info = PLANS[plan_key]
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{
            "price_data": {
                "currency": "dkk",
                "product_data": {
                    "name": f"UgoingViral {plan_info['name']}",
                    "description": f"{plan_info['credits']} credits per month",
                },
                "unit_amount": plan_info["price"] * 100,
                "recurring": {"interval": "month"},
            },
            "quantity": 1,
        }],
        client_reference_id=current_user["id"],
        customer_email=current_user["email"],
        success_url=f"{BASE_URL}/app?payment=success&plan={plan_key}",
        cancel_url=f"{BASE_URL}/app?payment=cancelled",
        metadata={"type": "plan", "plan": plan_key, "user_id": current_user["id"]},
        locale="da",
    )
    return {"url": session.url}


@router.post("/api/billing/webhook")
async def stripe_webhook(request: Request):
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")

    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Ugyldig webhook signatur")
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook fejl")

    if event["type"] == "checkout.session.completed":
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        user_id = obj.get("client_reference_id") or meta.get("user_id")
        payment_type = meta.get("type", "plan")

        if user_id and payment_type == "addon":
            addon_key = meta.get("addon", "")
            sub_slots = int(meta.get("sub_slots", 0))
            if sub_slots > 0:
                user_store = _load_user_store(user_id)
                billing = user_store.setdefault("billing", {})
                billing["extra_sub_slots"] = billing.get("extra_sub_slots", 0) + sub_slots
                user_store["billing"] = billing
                _save_user_store(user_id, user_store)

        elif user_id and payment_type in ("credits", "studio_package", "custom_topup"):
            credits = int(meta.get("credits", 0))
            if credits > 0:
                user_store = _load_user_store(user_id)
                billing = user_store.setdefault("billing", {})
                plan_key = billing.get("plan", "free")
                plan_info = PLANS.get(plan_key, PLANS["free"])
                billing["credits"] = billing.get("credits", plan_info["credits"]) + credits
                # Track purchased packages
                if payment_type == "studio_package":
                    pkg_key = meta.get("package", "")
                    pkgs = billing.setdefault("studio_packages", [])
                    if pkg_key and pkg_key not in pkgs:
                        pkgs.append(pkg_key)
                user_store["billing"] = billing
                _save_user_store(user_id, user_store)

        elif user_id and payment_type == "plan":
            plan_key = meta.get("plan")
            if plan_key and plan_key in PLANS:
                plan_info = PLANS[plan_key]
                user_store = _load_user_store(user_id)
                billing = user_store.setdefault("billing", {})
                # Founding members keep their locked plan — skip downgrade
                if not billing.get("founding_member"):
                    billing["plan"] = plan_key
                billing["credits"] = plan_info["credits"]
                billing["stripe_customer_email"] = obj.get("customer_email", "")
                user_store["billing"] = billing
                _save_user_store(user_id, user_store)
                # Send confirmation email
                try:
                    from services.users import get_user_by_id
                    from routes.email import send_payment_confirmation_email
                    u = get_user_by_id(user_id)
                    if u:
                        send_payment_confirmation_email(u["email"], u.get("name",""), plan_info["name"], plan_info["price"])
                except Exception:
                    pass
                if plan_key == "personal":
                    try:
                        from routes.admin import _auto_assign_personal
                        _auto_assign_personal(user_id)
                    except Exception:
                        pass
                # Award referral bonus to referrer on first paid upgrade
                try:
                    referred_by = user_store.get("referred_by", "")
                    if referred_by and not user_store.get("referral_bonus_paid"):
                        ref_store = _load_user_store(referred_by)
                        if ref_store:
                            ref_billing = ref_store.setdefault("billing", {})
                            ref_plan = PLANS.get(ref_billing.get("plan", "free"), PLANS["free"])
                            ref_billing["credits"] = ref_billing.get("credits", ref_plan["credits"]) + BONUS_CREDITS["invite_friend"]
                            ref_store["billing"] = ref_billing
                            upgraded = ref_store.setdefault("upgraded_referrals", [])
                            if user_id not in upgraded:
                                upgraded.append(user_id)
                            ref_store["upgraded_referrals"] = upgraded
                            _save_user_store(referred_by, ref_store)
                            user_store["referral_bonus_paid"] = True
                            _save_user_store(user_id, user_store)
                except Exception:
                    pass

    elif event["type"] == "customer.subscription.deleted":
        # Subscription cancelled — revert to free plan
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        user_id = meta.get("user_id")
        if not user_id:
            cid = obj.get("customer")
            if cid:
                try:
                    customers = stripe.Customer.list(limit=100)
                    for c in customers.auto_paging_iter():
                        if c.id == cid:
                            user_id = c.metadata.get("user_id")
                            break
                except Exception:
                    pass
        if user_id:
            user_store = _load_user_store(user_id)
            billing = user_store.setdefault("billing", {})
            if not billing.get("founding_member"):
                billing["plan"] = "free"
                billing["credits"] = PLANS["free"]["credits"]
            user_store["billing"] = billing
            _save_user_store(user_id, user_store)
            try:
                from services.users import get_user_by_id
                from routes.email import send_subscription_cancelled_email
                u = get_user_by_id(user_id)
                if u:
                    send_subscription_cancelled_email(u["email"], u.get("name",""))
            except Exception:
                pass

    elif event["type"] == "invoice.payment_failed":
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        user_id = obj.get("client_reference_id") or meta.get("user_id")
        if user_id:
            try:
                from services.users import get_user_by_id
                from routes.email import send_payment_failed_email
                u = get_user_by_id(user_id)
                if u:
                    send_payment_failed_email(u["email"], u.get("name",""))
            except Exception:
                pass

    elif event["type"] == "invoice.payment_succeeded":
        # Recurring subscription renewal — top up credits
        obj = event["data"]["object"]
        meta = obj.get("metadata") or {}
        user_id = obj.get("client_reference_id") or meta.get("user_id")
        plan_key = meta.get("plan")

        if not plan_key:
            sub_id = obj.get("subscription")
            if sub_id:
                try:
                    sub = stripe.Subscription.retrieve(sub_id)
                    sub_meta = sub.get("metadata", {})
                    plan_key = sub_meta.get("plan")
                    if not user_id:
                        user_id = sub_meta.get("user_id")
                except Exception:
                    pass

        if user_id and plan_key and plan_key in PLANS:
            plan_info = PLANS[plan_key]
            user_store = _load_user_store(user_id)
            billing = user_store.setdefault("billing", {})
            billing["plan"] = plan_key
            billing["credits"] = billing.get("credits", 0) + plan_info["credits"]
            user_store["billing"] = billing
            _save_user_store(user_id, user_store)

    return {"ok": True}


# ── Invite / Referral ──────────────────────────────────────────────────────────

@router.get("/api/billing/invite")
def get_invite(current_user: dict = Depends(get_current_user)):
    import hashlib
    uid = current_user["id"]
    code = hashlib.sha256(uid.encode()).hexdigest()[:10]
    ustore = _load_user_store(uid)
    invited = ustore.get("invited_users", [])
    return {
        "code": code,
        "url": f"https://ugoingviral.com/?ref={code}",
        "invited_count": len(invited),
        "credits_earned": len(invited) * BONUS_CREDITS.get("invite_friend", 50),
    }




# ── Founding Member ───────────────────────────────────────────────────────────

@router.get("/api/billing/founding_status")
def get_founding_status(current_user: dict = Depends(get_current_user)):
    uid = current_user["id"]
    ustore = _load_user_store(uid)
    billing = ustore.get("billing", {})
    return {
        "founding_member": billing.get("founding_member", False),
        "locked_plan": billing.get("locked_plan", ""),
        "locked_price": billing.get("locked_price", 0),
    }

@router.post("/api/billing/referral_signup")
def referral_signup(body: dict):
    """Called internally when a new user registers with a ref code.
    Records referred_by in new user's store; bonus credited on first paid upgrade."""
    ref_code = body.get("ref_code", "")
    new_user_id = body.get("user_id", "")
    if not ref_code or not new_user_id:
        return {"ok": False}

    import hashlib
    from services.users import get_all_users
    try:
        all_users = get_all_users()
    except Exception:
        return {"ok": False}

    for u in all_users:
        uid = u.get("id", "")
        if hashlib.sha256(uid.encode()).hexdigest()[:10] == ref_code:
            # Track in referrer's store
            ustore = _load_user_store(uid)
            invited = ustore.setdefault("invited_users", [])
            if new_user_id not in invited:
                invited.append(new_user_id)
                ustore["invited_users"] = invited
                _save_user_store(uid, ustore)
            # Record referrer in new user's store (bonus paid on upgrade)
            new_store = _load_user_store(new_user_id)
            if not new_store.get("referred_by"):
                new_store["referred_by"] = uid
                _save_user_store(new_user_id, new_store)
            return {"ok": True}
    return {"ok": False}


@router.get("/api/billing/referral_stats")
def referral_stats(current_user: dict = Depends(get_current_user)):
    import hashlib
    uid = current_user["id"]
    code = hashlib.sha256(uid.encode()).hexdigest()[:10]
    ustore = _load_user_store(uid)
    invited_count = len(ustore.get("invited_users", []))
    upgraded_count = len(ustore.get("upgraded_referrals", []))
    credits_earned = upgraded_count * BONUS_CREDITS.get("invite_friend", 50)
    return {
        "ref_code": code,
        "ref_url": f"https://ugoingviral.com/?ref={code}",
        "invited_count": invited_count,
        "upgraded_count": upgraded_count,
        "credits_earned": credits_earned,
    }


# ── Auto Top-Up ───────────────────────────────────────────────────────────────

@router.get("/api/billing/auto_topup")
def get_auto_topup(current_user: dict = Depends(get_current_user)):
    billing = store.get("billing", {})
    return {
        "enabled":   billing.get("auto_topup_enabled", False),
        "threshold":  billing.get("auto_topup_threshold", 50),
        "amount":    billing.get("auto_topup_amount", 100),
        "last_topup": billing.get("auto_topup_last", None),
    }


@router.post("/api/billing/auto_topup")
async def save_auto_topup(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    billing = store.setdefault("billing", {})
    billing["auto_topup_enabled"]   = bool(d.get("enabled", False))
    billing["auto_topup_threshold"]  = max(10, min(500, int(d.get("threshold", 50))))
    billing["auto_topup_amount"]    = int(d.get("amount", 100))
    save_store()
    return {"ok": True, "auto_topup": {
        "enabled":  billing["auto_topup_enabled"],
        "threshold": billing["auto_topup_threshold"],
        "amount":   billing["auto_topup_amount"],
    }}
