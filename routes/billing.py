import os
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store

router = APIRouter()

PLANS = {
    "free": {
        "name": "Free", "price": 0, "credits": 50,
        "growth_accounts": 0, "sub_accounts": 0, "video": False, "live_support": False, "free_videos": 10,
        "features": ["50 credits/mo", "10 free videos", "1 platform", "AI text content", "Basic scheduling"],
    },
    "starter": {
        "name": "Starter", "price": 49, "credits": 300,
        "growth_accounts": 0, "sub_accounts": 0, "video": False, "live_support": False, "free_videos": 10,
        "features": ["Everything in Free +", "300 credits/mo", "All platforms", "Auto-post & scheduling", "Content Pipeline", "1 main account"],
    },
    "basic": {
        "name": "Basic", "price": 79, "credits": 700,
        "growth_accounts": 0, "sub_accounts": 0, "video": False, "live_support": False, "free_videos": 10,
        "features": ["Everything in Starter +", "700 credits/mo", "Media library", "Content Mapper", "Analytics", "Priority queue"],
    },
    "pro": {
        "name": "Pro", "price": 149, "credits": 1600,
        "growth_accounts": 1, "sub_accounts": 0, "video": True, "live_support": True, "free_videos": 10,
        "features": ["Everything in Basic +", "1,600 credits/mo", "1 growth account", "AI video generation", "AI voiceover", "Advanced analytics", "Live chat support"],
    },
    "elite": {
        "name": "Elite", "price": 229, "credits": 2500,
        "growth_accounts": 3, "sub_accounts": 0, "video": True, "live_support": True, "free_videos": 10,
        "features": ["Everything in Pro +", "2,500 credits/mo", "3 growth accounts", "Priority video rendering", "Bulk scheduling", "Dedicated support"],
    },
    "personal": {
        "name": "Personal", "price": 459, "credits": 5000,
        "growth_accounts": 5, "sub_accounts": 999, "video": True, "live_support": True, "free_videos": 10,
        "features": ["Everything in Elite +", "5,000 credits/mo", "5 growth accounts", "Dedicated AI assistant", "White-label", "Custom integrations", "SLA"],
    },
    "growth": {
        "name": "Growth", "price": 89, "credits": 500,
        "growth_accounts": 2, "sub_accounts": 0, "video": False, "live_support": False, "free_videos": 10,
        "features": ["Everything in Starter +", "500 credits/mo", "2 growth accounts", "Auto DM replies", "Auto comment replies", "Engagement targeting", "Follower growth", "Anti-ban system"],
    },
    "agency": {
        "name": "Agency", "price": 199, "credits": 2000,
        "growth_accounts": 0, "sub_accounts": 10, "video": True, "live_support": True, "free_videos": 10,
        "features": ["Everything in Pro +", "2,000 credits/mo", "10 client accounts", "Agency dashboard", "Per-client content settings", "White-label", "Bulk scheduling"],
    },
}

PLAN_PRICES = {
    "starter":  os.getenv("STRIPE_PRICE_STARTER", ""),
    "growth":   os.getenv("STRIPE_PRICE_GROWTH", ""),
    "basic":    os.getenv("STRIPE_PRICE_BASIC", ""),
    "pro":      os.getenv("STRIPE_PRICE_PRO", ""),
    "elite":    os.getenv("STRIPE_PRICE_ELITE", ""),
    "personal": os.getenv("STRIPE_PRICE_PERSONAL", ""),
    "agency":   os.getenv("STRIPE_PRICE_AGENCY", ""),
}

BONUS_CREDITS = {
    "connect_social":  10,
    "first_video":     20,
    "first_post":      15,
    "daily_login":      5,
    "invite_friend":   50,
}

BASE_URL = "https://ugoingviral.com"

TOPUP_PACKAGES = {
    100:  {"price": 8,  "name": "100 Credits",   "note": "Quick refill",         "stripe_price_id": "price_1TTWHv5i1szYGmpZraRUCExx"},
    350:  {"price": 24, "name": "350 Credits",   "note": "Good value",            "stripe_price_id": "price_1TTWHv5i1szYGmpZvJpxn2D3"},
    700:  {"price": 44, "name": "700 Credits",   "note": "Most popular",          "stripe_price_id": "price_1TTWHw5i1szYGmpZW6PxPC6H"},
    1500: {"price": 79, "name": "1,500 Credits", "note": "Best price per credit", "stripe_price_id": "price_1TTWHw5i1szYGmpZCE7jZfpb"},
}

STUDIO_PACKAGES = {
    "serie_3": {
        "name": "3-Scene Series",
        "price": 29,
        "credits": 0,
        "icon": "🎬",
        "color": "#00e5ff",
        "description": "Perfect for a short product or brand series",
        "features": [
            "Up to 3 scenes per series",
            "Smart Clip Cutter — 6 clips",
            "2 output formats (9:16 + 16:9)",
            "ElevenLabs voiceover included",
            "AI prompt optimization via Claude",
            "Auto-upload to selected platform",
            "Uses credits from your balance",
        ],
        "scenes": 3, "clip_count": 6, "formats": 2,
    },
    "serie_6": {
        "name": "6-Scene Series",
        "price": 59,
        "credits": 0,
        "icon": "🎞️",
        "color": "#7c3aed",
        "description": "Tell a full story — ideal for campaigns",
        "features": [
            "Up to 6 scenes per series",
            "Smart Clip Cutter — up to 10 clips",
            "All 4 formats (9:16, 16:9, 1:1, 4:5)",
            "ElevenLabs voiceover included",
            "AI prompt optimization + style consistency",
            "Product blend in all scenes",
            "Auto-upload to all platforms",
            "Uses credits from your balance",
        ],
        "scenes": 6, "clip_count": 10, "formats": 4,
    },
    "serie_10": {
        "name": "10-Scene Studio",
        "price": 99,
        "credits": 0,
        "icon": "🏆",
        "color": "#f59e0b",
        "description": "Professional mini-series — brand set or content campaign",
        "features": [
            "Up to 10 scenes per series",
            "Smart Clip Cutter — up to 12 clips",
            "All 4 formats including 4:5",
            "ElevenLabs voiceover included",
            "AI prompt optimization (Claude Sonnet)",
            "Product blend in all scenes",
            "Multi-provider: Runway + Luma + Replicate",
            "Priority rendering",
            "Auto-upload to all platforms",
            "Uses credits from your balance",
        ],
        "scenes": 10, "clip_count": 12, "formats": 4,
    },
}

ADDON_PACKAGES = {
    "extra_profile": {"name": "Extra Profile", "price": 19, "sub_slots": 1, "icon": "👥",
                      "description": "Add 1 extra profile slot — $19 (all plans)",
                      "stripe_price_id": "price_1TTV9W5i1szYGmpZh9FxAaH8"},
}

# Growth account add-on packs
GROWTH_ACCOUNT_PACKS = {
    "growth_1":  {"name": "1 Growth Account",   "price": 19,  "slots": 1,  "icon": "📈",    "stripe_price_id": "price_1TTV9Q5i1szYGmpZ3RYAj2u7"},
    "growth_5":  {"name": "5 Growth Accounts",  "price": 79,  "slots": 5,  "icon": "📈📈",  "stripe_price_id": "price_1TTV9R5i1szYGmpZt4qutkls"},
    "growth_10": {"name": "10 Growth Accounts", "price": 139, "slots": 10, "icon": "📈📈📈", "stripe_price_id": "price_1TTV9R5i1szYGmpZaQi3seMi"},
    "growth_25": {"name": "25 Growth Accounts", "price": 299, "slots": 25, "icon": "📈x25", "stripe_price_id": "price_1TTV9S5i1szYGmpZBjqE8XkU"},
    "growth_50": {"name": "50 Growth Accounts", "price": 499, "slots": 50, "icon": "📈x50", "stripe_price_id": "price_1TTV9S5i1szYGmpZWCZxU8vF"},
}

# Agency extra client-account packs
AGENCY_CLIENT_PACKS = {
    "agency_client_1":  {"name": "1 Extra Client Account",   "price": 19,  "slots": 1,  "stripe_price_id": "price_1TTV9T5i1szYGmpZikha98Mo"},
    "agency_client_5":  {"name": "5 Extra Client Accounts",  "price": 79,  "slots": 5,  "stripe_price_id": "price_1TTV9U5i1szYGmpZeFKSpaqF"},
    "agency_client_10": {"name": "10 Extra Client Accounts", "price": 139, "slots": 10, "stripe_price_id": "price_1TTV9U5i1szYGmpZWalOXZAk"},
    "agency_client_25": {"name": "25 Extra Client Accounts", "price": 299, "slots": 25, "stripe_price_id": "price_1TTV9V5i1szYGmpZ7IOzRNz6"},
    "agency_client_50": {"name": "50 Extra Client Accounts", "price": 499, "slots": 50, "stripe_price_id": "price_1TTV9V5i1szYGmpZsB9XJdD3"},
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
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    pkg_key = body.get("package", "")
    if pkg_key not in STUDIO_PACKAGES:
        raise HTTPException(status_code=400, detail="Unknown studio package")

    pkg = STUDIO_PACKAGES[pkg_key]
    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
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
        locale="en",
    )
    return {"url": session.url}


@router.get("/api/billing/topup_packages")
def get_topup_packages(current_user: dict = Depends(get_current_user)):
    return {"packages": TOPUP_PACKAGES}


@router.post("/api/billing/create_custom_topup")
async def create_custom_topup(body: dict, current_user: dict = Depends(get_current_user)):
    """Custom credit top-up via Checkout Session. Rate: $0.053/credit. Min $79, no max."""
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    amount_usd = float(body.get("amount_usd", 0))
    if amount_usd < 79:
        raise HTTPException(status_code=400, detail="Minimum $79")

    credits = int(amount_usd / 0.053)
    uid = current_user["id"]

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=[{
            "price_data": {
                "currency": "usd",
                "product_data": {
                    "name": f"UgoingViral {credits:,} Credits",
                    "description": f"Custom top-up: {credits:,} credits at $0.053/credit",
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
        locale="en",
    )
    return {"url": session.url, "credits": credits}


@router.post("/api/billing/custom-topup")
async def custom_topup_intent(body: dict, current_user: dict = Depends(get_current_user)):
    """PaymentIntent-based custom top-up. Returns client_secret for Stripe Elements.
    Rate: $0.053/credit fixed. Min $79, no max.
    Example: $200 -> 3773 credits | $100 -> 1886 credits
    """
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe not configured")

    amount_usd = float(body.get("amount_usd", 0))
    if amount_usd < 79:
        raise HTTPException(status_code=400, detail="Minimum $79")

    amount_cents = int(round(amount_usd * 100))
    credits = int(amount_usd / 0.053)
    uid = current_user["id"]

    intent = stripe.PaymentIntent.create(
        amount=amount_cents,
        currency="usd",
        automatic_payment_methods={"enabled": True},
        metadata={"type": "custom_topup", "credits": str(credits), "user_id": uid},
        description=f"UgoingViral {credits:,} credits @ $0.053/credit",
    )
    return {
        "client_secret": intent.client_secret,
        "credits": credits,
        "amount_usd": amount_usd,
        "amount_cents": amount_cents,
    }


@router.get("/api/billing/addons")
def get_addons(current_user: dict = Depends(get_current_user)):
    billing = store.get("billing", {})
    return {
        "packages": ADDON_PACKAGES,
        "extra_sub_slots": billing.get("extra_sub_slots", 0),
        "subs_blocked": billing.get("subs_blocked", False),
    }


@router.get("/api/billing/growth_packs")
def get_growth_packs(current_user: dict = Depends(get_current_user)):
    billing = store.get("billing", {})
    return {
        "packs": GROWTH_ACCOUNT_PACKS,
        "extra_growth_slots": billing.get("extra_growth_slots", 0),
    }


@router.get("/api/billing/agency_client_packs")
def get_agency_client_packs(current_user: dict = Depends(get_current_user)):
    billing = store.get("billing", {})
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    base_slots = plan_info.get("sub_accounts", 0)
    extra_slots = billing.get("extra_agency_client_slots", 0)
    return {
        "packs": AGENCY_CLIENT_PACKS,
        "base_slots": base_slots,
        "extra_slots": extra_slots,
        "total_slots": base_slots + extra_slots,
    }


@router.post("/api/billing/create_addon_checkout")
async def create_addon_checkout(body: dict, current_user: dict = Depends(get_current_user)):
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe is not configured")

    pkg_key = body.get("package", "")
    # Support legacy keys -> unified extra_profile
    if pkg_key in ("extra_profile_starter", "extra_profile_pro"):
        pkg_key = "extra_profile"
    if pkg_key not in ADDON_PACKAGES:
        raise HTTPException(status_code=400, detail="Unknown add-on package")

    pkg = ADDON_PACKAGES[pkg_key]
    uid = current_user["id"]
    stripe_pid = pkg.get("stripe_price_id", "")

    if stripe_pid:
        line_items = [{"price": stripe_pid, "quantity": 1}]
    else:
        line_items = [{"price_data": {
            "currency": "usd",
            "product_data": {"name": f"UgoingViral {pkg['name']}", "description": pkg["description"]},
            "unit_amount": pkg["price"] * 100,
        }, "quantity": 1}]

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="payment",
        line_items=line_items,
        client_reference_id=uid,
        customer_email=current_user["email"],
        success_url=f"{BASE_URL}/app?payment=success&addon={pkg_key}&sub_slots={pkg['sub_slots']}",
        cancel_url=f"{BASE_URL}/app?payment=cancelled",
        metadata={"type": "addon", "addon": pkg_key, "sub_slots": str(pkg["sub_slots"]), "user_id": uid},
        locale="en",
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
        raise HTTPException(status_code=400, detail="Amount must be positive")
    billing = store.setdefault("billing", {})
    plan_key = billing.get("plan", "free")
    plan_info = PLANS.get(plan_key, PLANS["free"])
    current = billing.get("credits", plan_info["credits"])
    if current < req.amount:
        raise HTTPException(status_code=402, detail="Not enough credits")
    billing["credits"] = current - req.amount
    store["billing"] = billing
    save_store()
    return {"credits": billing["credits"], "used": req.amount}


def _try_auto_bonus(bonus_key: str, user_id: str = "") -> int:
    """Award bonus silently. Returns credits awarded (0 if already claimed)."""
    if bonus_key not in BONUS_CREDITS:
        return 0
    billing = store.setdefault("billing", {})
    claimed = billing.setdefault("claimed_bonuses", [])
    if bonus_key == "invite_friend":
        invite_count = billing.get("invite_bonus_count", 0)
        if invite_count >= 10:
            return 0
        billing["invite_bonus_count"] = invite_count + 1
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
        raise HTTPException(status_code=400, detail="Unknown bonus")
    billing = store.setdefault("billing", {})
    claimed = billing.setdefault("claimed_bonuses", [])
    if bonus_key == "invite_friend":
        invite_count = billing.get("invite_bonus_count", 0)
        if invite_count >= 10:
            raise HTTPException(status_code=409, detail="Maximum invite bonuses reached")
        billing["invite_bonus_count"] = invite_count + 1
    elif bonus_key in claimed:
        raise HTTPException(status_code=409, detail="Bonus already claimed")
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
        raise HTTPException(status_code=400, detail="Unknown plan")
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
        raise HTTPException(status_code=503, detail="PayPal not configured — add PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET to .env")

    import httpx as _httpx
    async with _httpx.AsyncClient() as c:
        token_r = await c.post(
            "https://api-m.paypal.com/v1/oauth2/token",
            headers={"Accept": "application/json"},
            auth=(paypal_client_id, paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        if token_r.status_code != 200:
            raise HTTPException(status_code=503, detail="PayPal token error")
        access_token = token_r.json()["access_token"]

    payment_type = body.get("type", "plan")
    uid = current_user["id"]

    if payment_type == "credits":
        credits = int(body.get("credits", 0))
        if credits not in TOPUP_PACKAGES:
            raise HTTPException(status_code=400, detail="Invalid credits package")
        pkg = TOPUP_PACKAGES[credits]
        amount_usd = str(pkg["price"])
        description = f"UgoingViral {pkg['name']} — {credits} credits"
        custom_id = f"credits_{credits}_{uid}"
    else:
        plan_key = body.get("plan", "")
        if plan_key not in PLANS or PLANS[plan_key]["price"] == 0:
            raise HTTPException(status_code=400, detail="Invalid plan")
        plan_info = PLANS[plan_key]
        amount_usd = str(plan_info["price"])
        description = f"UgoingViral {plan_info['name']} — {plan_info['credits']} credits/mo"
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
                    "amount": {"currency_code": "USD", "value": amount_usd},
                }],
                "application_context": {
                    "brand_name": "UgoingViral",
                    "locale": "en-US",
                    "return_url": f"{BASE_URL}/app?payment=success&provider=paypal&type={payment_type}&ref={custom_id}",
                    "cancel_url": f"{BASE_URL}/app?payment=cancelled",
                },
            },
        )
        if order_r.status_code not in (200, 201):
            raise HTTPException(status_code=503, detail=f"PayPal order error: {order_r.text[:200]}")
        order = order_r.json()

    approve_url = next((l["href"] for l in order.get("links", []) if l["rel"] == "approve"), None)
    if not approve_url:
        raise HTTPException(status_code=503, detail="PayPal returned no approval URL")
    return {"url": approve_url, "order_id": order["id"]}


@router.post("/api/billing/paypal_capture")
async def paypal_capture(body: dict, current_user: dict = Depends(get_current_user)):
    """Capture PayPal payment after user approves — applies credits/plan."""
    paypal_client_id     = os.getenv("PAYPAL_CLIENT_ID", "")
    paypal_client_secret = os.getenv("PAYPAL_CLIENT_SECRET", "")
    order_id = body.get("order_id", "")
    custom_id = body.get("custom_id", "")
    if not order_id:
        raise HTTPException(status_code=400, detail="Missing order_id")

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
            raise HTTPException(status_code=400, detail="PayPal capture failed")
        cap_data = cap_r.json()

    status = cap_data.get("status", "")
    if status != "COMPLETED":
        raise HTTPException(status_code=400, detail=f"Payment not completed: {status}")

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

    raise HTTPException(status_code=400, detail="Unknown PayPal payment")


@router.post("/api/billing/create_checkout")
async def create_checkout(body: dict, current_user: dict = Depends(get_current_user)):
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key:
        raise HTTPException(status_code=503, detail="Stripe is not configured — add STRIPE_SECRET_KEY to .env")

    payment_type = body.get("type", "plan")

    # ── One-time credit top-up ───────────────────────────────────────────────────────────────────────
    if payment_type == "credits":
        credits = int(body.get("credits", 0))
        if credits not in TOPUP_PACKAGES:
            raise HTTPException(status_code=400, detail="Invalid credits package")
        pkg = TOPUP_PACKAGES[credits]
        uid = current_user["id"]
        stripe_pid = pkg.get("stripe_price_id", "")
        if stripe_pid:
            line_items = [{"price": stripe_pid, "quantity": 1}]
        else:
            line_items = [{"price_data": {
                "currency": "usd",
                "product_data": {"name": f"UgoingViral {pkg['name']}", "description": f"One-time purchase of {credits} credits"},
                "unit_amount": pkg["price"] * 100,
            }, "quantity": 1}]
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=line_items,
            client_reference_id=uid,
            customer_email=current_user["email"],
            success_url=f"{BASE_URL}/app?payment=success&credits={credits}",
            cancel_url=f"{BASE_URL}/app?payment=cancelled",
            metadata={"type": "credits", "credits": str(credits), "user_id": uid},
            locale="en",
        )
        return {"url": session.url}

    # ── Growth account pack (one-time) ──────────────────────────────────────
    if payment_type == "growth_pack":
        pack_key = body.get("pack", "")
        if pack_key not in GROWTH_ACCOUNT_PACKS:
            raise HTTPException(status_code=400, detail="Unknown growth pack")
        pack = GROWTH_ACCOUNT_PACKS[pack_key]
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{"price_data": {"currency": "usd", "product_data": {"name": f"UgoingViral {pack['name']}"}, "unit_amount": pack["price"] * 100}, "quantity": 1}],
            client_reference_id=current_user["id"],
            customer_email=current_user["email"],
            success_url=f"{BASE_URL}/app?payment=success&pack={pack_key}",
            cancel_url=f"{BASE_URL}/app?payment=cancelled",
            metadata={"type": "growth_pack", "pack": pack_key, "slots": str(pack["slots"]), "user_id": current_user["id"]},
            locale="en",
        )
        return {"url": session.url}

    # ── Agency client account pack (one-time) ────────────────────────────────
    if payment_type == "agency_client_pack":
        pack_key = body.get("pack", "")
        if pack_key not in AGENCY_CLIENT_PACKS:
            raise HTTPException(status_code=400, detail="Unknown agency client pack")
        pack = AGENCY_CLIENT_PACKS[pack_key]
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="payment",
            line_items=[{"price_data": {"currency": "usd", "product_data": {"name": f"UgoingViral {pack['name']}"}, "unit_amount": pack["price"] * 100}, "quantity": 1}],
            client_reference_id=current_user["id"],
            customer_email=current_user["email"],
            success_url=f"{BASE_URL}/app?payment=success&pack={pack_key}",
            cancel_url=f"{BASE_URL}/app?payment=cancelled",
            metadata={"type": "agency_client_pack", "pack": pack_key, "slots": str(pack["slots"]), "user_id": current_user["id"]},
            locale="en",
        )
        return {"url": session.url}

    # ── Subscription plan ────────────────────────────────────────────────────
    plan_key = body.get("plan", "")
    if plan_key not in PLANS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    if PLANS[plan_key]["price"] == 0:
        raise HTTPException(status_code=400, detail="Free plan does not require payment")

    price_id = PLAN_PRICES.get(plan_key, "")
    if not price_id:
        raise HTTPException(status_code=503, detail=f"No Stripe price ID configured for plan '{plan_key}'")

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        client_reference_id=current_user["id"],
        customer_email=current_user["email"],
        success_url=f"{BASE_URL}/app?payment=success&plan={plan_key}",
        cancel_url=f"{BASE_URL}/app?payment=cancelled",
        metadata={"type": "plan", "plan": plan_key, "user_id": current_user["id"]},
        subscription_data={"metadata": {"plan": plan_key, "user_id": current_user["id"]}},
        locale="en",
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
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    except Exception:
        raise HTTPException(status_code=400, detail="Webhook error")

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

        elif user_id and payment_type == "growth_pack":
            slots = int(meta.get("slots", 0))
            if slots > 0:
                user_store = _load_user_store(user_id)
                billing = user_store.setdefault("billing", {})
                billing["extra_growth_slots"] = billing.get("extra_growth_slots", 0) + slots
                user_store["billing"] = billing
                _save_user_store(user_id, user_store)

        elif user_id and payment_type == "agency_client_pack":
            slots = int(meta.get("slots", 0))
            if slots > 0:
                user_store = _load_user_store(user_id)
                billing = user_store.setdefault("billing", {})
                billing["extra_agency_client_slots"] = billing.get("extra_agency_client_slots", 0) + slots
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
                if not billing.get("founding_member"):
                    billing["plan"] = plan_key
                billing["credits"] = plan_info["credits"]
                billing["stripe_customer_email"] = obj.get("customer_email", "")
                user_store["billing"] = billing
                _save_user_store(user_id, user_store)
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
                try:
                    referred_by = user_store.get("referred_by", "")
                    if referred_by and not user_store.get("referral_bonus_paid"):
                        ref_store = _load_user_store(referred_by)
                        if ref_store:
                            ref_billing = ref_store.setdefault("billing", {})
                            ref_plan = PLANS.get(ref_billing.get("plan", "free"), PLANS["free"])
                            ref_invite_count = ref_billing.get("invite_bonus_count", 0)
                            if ref_invite_count < 10:
                                ref_billing["credits"] = ref_billing.get("credits", ref_plan["credits"]) + BONUS_CREDITS["invite_friend"]
                                ref_billing["invite_bonus_count"] = ref_invite_count + 1
                                ref_store["billing"] = ref_billing
                                upgraded = ref_store.setdefault("upgraded_referrals", [])
                                if user_id not in upgraded:
                                    upgraded.append(user_id)
                                ref_store["upgraded_referrals"] = upgraded
                                _save_user_store(referred_by, ref_store)
                                user_store["referral_bonus_paid"] = True
                                _save_user_store(user_id, user_store)
                                try:
                                    from routes.notifications import push_notification
                                    push_notification(
                                        referred_by, "referral_upgraded",
                                        "Referral upgraded!",
                                        "Someone you referred just upgraded to a paid plan. You earned 50 bonus credits!"
                                    )
                                except Exception:
                                    pass
                except Exception:
                    pass

    elif event["type"] == "customer.subscription.deleted":
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
    """Called internally when a new user registers with a ref code."""
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
            ustore = _load_user_store(uid)
            invited = ustore.setdefault("invited_users", [])
            if new_user_id not in invited:
                invited.append(new_user_id)
                ustore["invited_users"] = invited
                _save_user_store(uid, ustore)
            new_store = _load_user_store(new_user_id)
            if not new_store.get("referred_by"):
                new_store["referred_by"] = uid
                _save_user_store(new_user_id, new_store)
            return {"ok": True}

    # Max 10 invite bonuses enforced in webhook
    billing = store.get("billing", {})
    invite_count = billing.get("invite_bonus_count", 0)
    if invite_count >= 10:
        return {"ok": False, "reason": "invite_limit_reached"}

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
        "enabled":    billing.get("auto_topup_enabled", False),
        "threshold":  billing.get("auto_topup_threshold", 50),
        "amount":     billing.get("auto_topup_amount", 100),
        "last_topup": billing.get("auto_topup_last", None),
    }


@router.post("/api/billing/auto_topup")
async def save_auto_topup(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    billing = store.setdefault("billing", {})
    billing["auto_topup_enabled"]  = bool(d.get("enabled", False))
    billing["auto_topup_threshold"] = max(10, min(500, int(d.get("threshold", 50))))
    billing["auto_topup_amount"]   = int(d.get("amount", 100))
    save_store()
    return {"ok": True, "auto_topup": {
        "enabled":   billing["auto_topup_enabled"],
        "threshold": billing["auto_topup_threshold"],
        "amount":    billing["auto_topup_amount"],
    }}
