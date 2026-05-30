"""
UgoingViral Affiliate Program

Affiliate tiers based on customer count:
- Bronze: 1-9 customers   -> 10% commission
- Silver: 10-19 customers -> 15% commission
- Gold:   20+ customers   -> 20% commission

Commissions are held 30 days after payment for refund protection, then become
available for payout. Payouts run on the 1st of every month via PayPal / Wise
with a $50 minimum threshold.
"""
import os
import json
import hashlib
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException, Depends
from routes.auth import get_current_user

router = APIRouter()

_DIR = os.path.dirname(os.path.abspath(__file__))
AFFILIATES_FILE = os.path.join(_DIR, "..", "affiliates.json")
APPS_FILE = os.path.join(_DIR, "..", "affiliate_applications.json")
STATS_FILE = os.path.join(_DIR, "..", "users.json")

BASE_URL = "https://ugoingviral.com"
HOLD_DAYS = 30
MIN_PAYOUT_USD = 50


def _tier_for(customer_count: int) -> dict:
    if customer_count >= 20:
        return {"key": "gold", "name": "Gold", "icon": "🥇", "rate": 20, "next": None, "next_label": None}
    if customer_count >= 10:
        return {"key": "silver", "name": "Silver", "icon": "🥈", "rate": 15, "next": 20, "next_label": "Gold"}
    return {"key": "bronze", "name": "Bronze", "icon": "🥉", "rate": 10, "next": 10, "next_label": "Silver"}


# Partner tiers an admin can assign manually (overrides the automatic
# customer-count tier above). Standard 10%, Silver 20%, Gold 30%.
MANUAL_TIERS = {
    "standard": {"key": "standard", "name": "Standard", "icon": "⭐", "rate": 10},
    "silver":   {"key": "silver",   "name": "Silver",   "icon": "🥈", "rate": 20},
    "gold":     {"key": "gold",     "name": "Gold",     "icon": "🥇", "rate": 30},
}


def _resolve_tier(aff: dict, customer_count: int) -> dict:
    """Return the affiliate's effective tier. An admin-assigned `manual_tier`
    takes precedence over the automatic customer-count tier so partner deals
    (Standard/Silver/Gold) are honoured in every commission calculation."""
    manual = (aff.get("manual_tier") or "").lower()
    if manual in MANUAL_TIERS:
        tier = dict(MANUAL_TIERS[manual])
        tier.update({"next": None, "next_label": None, "manual": True})
        return tier
    return _tier_for(customer_count)


def _load_affiliates() -> dict:
    if os.path.exists(AFFILIATES_FILE):
        try:
            with open(AFFILIATES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"affiliates": {}}


def _save_affiliates(data: dict) -> None:
    with open(AFFILIATES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_apps() -> list:
    if os.path.exists(APPS_FILE):
        try:
            with open(APPS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_apps(apps: list) -> None:
    with open(APPS_FILE, "w", encoding="utf-8") as f:
        json.dump(apps, f, indent=2, ensure_ascii=False)


def _aff_code(user_id: str) -> str:
    return hashlib.sha256(("aff_" + user_id).encode()).hexdigest()[:12]


def _classify_commissions(commissions: list) -> dict:
    """Buckets commissions by hold-status and totals each bucket. Mutates entries
    in-place by promoting pending -> available once the 30-day hold has lapsed."""
    now = datetime.utcnow()
    pending = 0.0
    available = 0.0
    paid = 0.0
    for c in commissions:
        amount = float(c.get("amount", 0) or 0)
        status = c.get("status", "pending")
        if status == "paid":
            paid += amount
            continue
        if status == "available":
            available += amount
            continue
        try:
            created = datetime.fromisoformat(c.get("date", "").replace("Z", ""))
        except Exception:
            created = now
        if (now - created).days >= HOLD_DAYS:
            c["status"] = "available"
            available += amount
        else:
            pending += amount
    return {
        "pending": round(pending, 2),
        "available": round(available, 2),
        "paid": round(paid, 2),
        "total_earned": round(pending + available + paid, 2),
    }


def _next_payout_date() -> str:
    now = datetime.utcnow()
    if now.month == 12:
        nf = datetime(now.year + 1, 1, 1)
    else:
        nf = datetime(now.year, now.month + 1, 1)
    return nf.strftime("%Y-%m-%d")


# ── Public endpoints ──────────────────────────────────────────────────────────

@router.get("/api/affiliate/me")
def my_affiliate(current_user: dict = Depends(get_current_user)):
    """Returns affiliate status + dashboard data for the logged-in user."""
    uid = current_user["id"]
    data = _load_affiliates()
    aff = data["affiliates"].get(uid)

    if not aff:
        apps = _load_apps()
        for a in apps:
            if a.get("user_id") == uid or a.get("email", "").lower() == current_user.get("email", "").lower():
                return {"status": a.get("status", "pending")}
        return {"status": "none"}

    status = aff.get("status", "pending")
    if status != "approved":
        return {"status": status}

    commissions = aff.get("commissions", [])
    totals = _classify_commissions(commissions)
    aff["commissions"] = commissions
    _save_affiliates(data)

    customer_count = len(set(c.get("customer_email", "") for c in commissions if c.get("customer_email")))
    tier = _resolve_tier(aff, customer_count)
    code = _aff_code(uid)
    return {
        "status": "approved",
        "tier": tier,
        "customer_count": customer_count,
        "commissions": commissions,
        "total_earned": totals["total_earned"],
        "pending_amount": totals["pending"],
        "available_amount": totals["available"],
        "paid_amount": totals["paid"],
        "ref_url": f"{BASE_URL}/?aff={code}",
        "code": code,
        "payout_method": aff.get("payout_method", "paypal"),
        "payout_info": aff.get("payout_info", ""),
    }


@router.post("/api/affiliate/apply")
async def affiliate_apply(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip()
    payout_method = str(body.get("payout_method", "paypal")).strip().lower()
    payout_info = str(body.get("payout_info", "")).strip()
    how = str(body.get("how", "")).strip()
    website = str(body.get("website", "")).strip()
    reach = str(body.get("monthly_reach", "")).strip()
    niche = str(body.get("niche", "")).strip()

    if not name or not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Name and valid email are required")
    if payout_method not in ("paypal", "wise"):
        payout_method = "paypal"
    if not payout_info:
        raise HTTPException(status_code=422, detail="Payout info is required")
    if not how:
        raise HTTPException(status_code=422, detail="Please describe how you'll promote")

    user_id = ""
    try:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            from routes.auth import SECRET_KEY, ALGORITHM
            from jose import jwt as _jwt
            payload = _jwt.decode(auth[7:], SECRET_KEY, algorithms=[ALGORITHM])
            user_id = payload.get("user_id", "")
    except Exception:
        user_id = ""

    apps = _load_apps()
    if any(a.get("email", "").lower() == email.lower() and a.get("status") == "pending" for a in apps):
        return {"ok": True, "message": "Application already received — we'll be in touch soon."}

    apps.append({
        "name": name,
        "email": email,
        "user_id": user_id,
        "payout_method": payout_method,
        "payout_info": payout_info,
        "how": how,
        "website": website,
        "monthly_reach": reach,
        "niche": niche,
        "applied_at": datetime.utcnow().isoformat() + "Z",
        "status": "pending",
    })
    _save_apps(apps)

    try:
        from routes.email import send_system_email
        send_system_email(
            "support@ugoingviral.com",
            f"New Affiliate Application — {name}",
            f"<p><strong>Name:</strong> {name}<br>"
            f"<strong>Email:</strong> {email}<br>"
            f"<strong>Payout:</strong> {payout_method} — {payout_info}<br>"
            f"<strong>Promotion plan:</strong> {how}</p>",
        )
    except Exception:
        pass

    return {"ok": True, "message": "Application received! We'll review it within 48 hours."}


@router.post("/api/affiliate/payout_info")
async def update_payout_info(request: Request, current_user: dict = Depends(get_current_user)):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    method = str(body.get("payout_method", "paypal")).lower()
    info = str(body.get("payout_info", "")).strip()
    if method not in ("paypal", "wise"):
        method = "paypal"
    if not info:
        raise HTTPException(status_code=422, detail="Payout info is required")
    data = _load_affiliates()
    aff = data["affiliates"].get(current_user["id"])
    if not aff:
        raise HTTPException(status_code=404, detail="Not an approved affiliate")
    aff["payout_method"] = method
    aff["payout_info"] = info
    _save_affiliates(data)
    return {"ok": True}


@router.get("/api/public/stats")
def public_stats():
    """Public-safe platform stats for landing page social proof."""
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        total = len(data.get("users", []))
    except Exception:
        total = 0
    creator_count = max(total, 1) * 200 + 2300
    return {"creator_count": creator_count}


# ── Admin endpoints ───────────────────────────────────────────────────────────

def _require_owner_or_support(request: Request):
    from routes.admin import _decode_token
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    payload = _decode_token(auth[7:])
    if payload.get("role") not in ("owner", "support"):
        raise HTTPException(status_code=403, detail="Owner / support access required")
    return payload


@router.get("/api/admin/affiliates")
def admin_list_affiliates(request: Request):
    _require_owner_or_support(request)
    data = _load_affiliates()
    apps = _load_apps()

    out_aff = []
    for uid, aff in data["affiliates"].items():
        commissions = aff.get("commissions", [])
        totals = _classify_commissions(commissions)
        customer_count = len(set(c.get("customer_email", "") for c in commissions if c.get("customer_email")))
        tier = _resolve_tier(aff, customer_count)
        out_aff.append({
            "user_id": uid,
            "name": aff.get("name", ""),
            "email": aff.get("email", ""),
            "status": aff.get("status", "approved"),
            "tier": tier,
            "manual_tier": (aff.get("manual_tier") or "").lower(),
            "customer_count": customer_count,
            "payout_method": aff.get("payout_method", ""),
            "payout_info": aff.get("payout_info", ""),
            "pending": totals["pending"],
            "available": totals["available"],
            "paid": totals["paid"],
            "total_earned": totals["total_earned"],
            "approved_at": aff.get("approved_at", ""),
        })

    pending_apps = [a for a in apps if a.get("status") == "pending"]
    monthly_total = round(sum(a["available"] for a in out_aff), 2)
    ready_count = sum(1 for a in out_aff if a["available"] >= MIN_PAYOUT_USD)
    return {
        "affiliates": out_aff,
        "applications": pending_apps,
        "monthly_overview": {
            "total_available": monthly_total,
            "ready_for_payout": ready_count,
            "min_payout_usd": MIN_PAYOUT_USD,
            "next_payout_date": _next_payout_date(),
        },
    }


@router.post("/api/admin/affiliates/approve")
async def admin_approve_application(request: Request):
    _require_owner_or_support(request)
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="email required")

    apps = _load_apps()
    target = None
    for a in apps:
        if a.get("email", "").lower() == email and a.get("status") == "pending":
            target = a
            break
    if not target:
        raise HTTPException(status_code=404, detail="Application not found")

    uid = target.get("user_id", "")
    if not uid:
        try:
            from services.users import get_user_by_email
            u = get_user_by_email(email)
            if u:
                uid = u["id"]
        except Exception:
            pass
    if not uid:
        uid = "aff_" + hashlib.sha256(email.encode()).hexdigest()[:16]

    data = _load_affiliates()
    data["affiliates"][uid] = {
        "name": target.get("name", ""),
        "email": email,
        "status": "approved",
        "payout_method": target.get("payout_method", "paypal"),
        "payout_info": target.get("payout_info", ""),
        "how": target.get("how", ""),
        "commissions": [],
        "approved_at": datetime.utcnow().isoformat() + "Z",
    }
    _save_affiliates(data)

    target["status"] = "approved"
    target["approved_at"] = datetime.utcnow().isoformat() + "Z"
    _save_apps(apps)

    try:
        from routes.email import send_system_email
        send_system_email(
            email,
            "You're approved — UgoingViral Affiliate Program",
            f"<p>Hi {target.get('name', '')},</p>"
            f"<p>Your affiliate application has been approved! Log in to your dashboard to grab your affiliate link and start earning.</p>"
            f"<p>Your link: <a href='{BASE_URL}/?aff={_aff_code(uid)}'>{BASE_URL}/?aff={_aff_code(uid)}</a></p>",
        )
    except Exception:
        pass
    return {"ok": True, "user_id": uid}


@router.post("/api/admin/affiliates/reject")
async def admin_reject_application(request: Request):
    _require_owner_or_support(request)
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    if not email:
        raise HTTPException(status_code=422, detail="email required")
    apps = _load_apps()
    found = False
    for a in apps:
        if a.get("email", "").lower() == email and a.get("status") == "pending":
            a["status"] = "rejected"
            a["rejected_at"] = datetime.utcnow().isoformat() + "Z"
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail="Application not found")
    _save_apps(apps)
    return {"ok": True}


@router.post("/api/admin/affiliates/mark_paid")
async def admin_mark_paid(request: Request):
    """Marks every currently-available commission for an affiliate as paid."""
    _require_owner_or_support(request)
    body = await request.json()
    uid = str(body.get("user_id", "")).strip()
    if not uid:
        raise HTTPException(status_code=422, detail="user_id required")

    data = _load_affiliates()
    aff = data["affiliates"].get(uid)
    if not aff:
        raise HTTPException(status_code=404, detail="Affiliate not found")

    paid_total = 0.0
    paid_at = datetime.utcnow().isoformat() + "Z"
    _classify_commissions(aff.get("commissions", []))
    for c in aff.get("commissions", []):
        if c.get("status") == "available":
            c["status"] = "paid"
            c["paid_at"] = paid_at
            paid_total += float(c.get("amount", 0) or 0)

    payouts = aff.setdefault("payouts", [])
    payouts.append({
        "amount": round(paid_total, 2),
        "paid_at": paid_at,
        "method": aff.get("payout_method", ""),
        "info": aff.get("payout_info", ""),
    })
    _save_affiliates(data)
    return {"ok": True, "paid_amount": round(paid_total, 2)}


@router.post("/api/admin/affiliates/set_tier")
async def admin_set_tier(request: Request):
    """Assign a partner tier to an affiliate (Standard 10% / Silver 20% / Gold 30%).
    The manual tier overrides the automatic customer-count tier and is applied to
    every future commission. Pass tier="" (or "auto") to clear the override."""
    _require_owner_or_support(request)
    body = await request.json()
    uid = str(body.get("user_id", "")).strip()
    tier = str(body.get("tier", "")).strip().lower()
    if not uid:
        raise HTTPException(status_code=422, detail="user_id required")
    if tier in ("", "auto", "none"):
        tier = ""
    elif tier not in MANUAL_TIERS:
        raise HTTPException(status_code=422, detail=f"tier must be one of {sorted(MANUAL_TIERS.keys())} (or empty to clear)")

    data = _load_affiliates()
    aff = data["affiliates"].get(uid)
    if not aff:
        raise HTTPException(status_code=404, detail="Affiliate not found")
    if tier:
        aff["manual_tier"] = tier
    else:
        aff.pop("manual_tier", None)
    _save_affiliates(data)

    customer_count = len(set(c.get("customer_email", "") for c in aff.get("commissions", []) if c.get("customer_email")))
    return {"ok": True, "user_id": uid, "manual_tier": tier, "tier": _resolve_tier(aff, customer_count)}


# ── Internal hook: record a commission when a referred user pays ──────────────

def record_commission(referrer_user_id: str, customer_email: str, plan: str, amount_usd: float) -> bool:
    """Called from the Stripe webhook (or top-up flow) to credit an affiliate.
    Calculates commission using the affiliate's current tier."""
    if not referrer_user_id or amount_usd <= 0:
        return False
    data = _load_affiliates()
    aff = data["affiliates"].get(referrer_user_id)
    if not aff or aff.get("status") != "approved":
        return False
    commissions = aff.setdefault("commissions", [])
    customer_count = len(set(c.get("customer_email", "") for c in commissions if c.get("customer_email")))
    tier = _resolve_tier(aff, customer_count + 1)
    commission = round(amount_usd * tier["rate"] / 100.0, 2)
    commissions.append({
        "date": datetime.utcnow().isoformat() + "Z",
        "customer_email": customer_email,
        "plan": plan,
        "payment_usd": round(amount_usd, 2),
        "amount": commission,
        "tier": tier["key"],
        "status": "pending",
    })
    _save_affiliates(data)
    return True
