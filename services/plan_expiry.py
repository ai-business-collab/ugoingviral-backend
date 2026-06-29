"""
Daily plan-expiry enforcement for GRANTED / campaign free periods.

The manual owner grant (/api/admin/quick_extend_plan) and promo campaigns set:
    billing["plan"]            = "<paid plan>"
    billing["plan_expires"]    = <ISO datetime>
    billing["plan_extended_by"]= "admin"  |  "campaign:<CODE>"

Real paying Stripe subscriptions NEVER set those fields — they use
current_period_end / stripe_subscription_id — so `plan_extended_by` is a reliable
"this is a complimentary grant" marker.

When a granted period ends we revert the user to the FREE plan (credits are left
untouched) and nudge them to upgrade. We NEVER downgrade a real paying
subscriber: if the user has since started an active Stripe subscription we leave
their plan alone and only clear the stale grant markers. The Stripe check fails
SAFE — if there is Stripe linkage we cannot verify, we do not downgrade.
"""
import os
import logging
from datetime import datetime

from services.store import get_all_user_ids, _load_user_store, _save_user_store

_LOG = logging.getLogger("ugv.plan_expiry")

_ACTIVE_SUB_STATUSES = ("active", "trialing", "past_due", "unpaid")


def _is_granted(billing: dict) -> bool:
    """True if this plan came from a complimentary grant / campaign (not a purchase)."""
    by = str(billing.get("plan_extended_by", "") or "")
    return by == "admin" or by.startswith("campaign:")


def _is_expired(billing: dict, now: datetime) -> bool:
    exp = billing.get("plan_expires")
    if not exp:
        return False
    try:
        dt = datetime.fromisoformat(str(exp).replace("Z", "").strip())
    except Exception:
        return False
    return now >= dt


def _has_active_paid_subscription(billing: dict) -> bool:
    """True if the user has a real, active Stripe subscription.

    Fail SAFE: if there is Stripe linkage (a stored subscription/customer id) we
    cannot verify (no API key, or an API error), assume they are paying so we
    never downgrade a real subscriber. No Stripe linkage at all → not paying."""
    sub_id = billing.get("stripe_subscription_id")
    cust   = billing.get("stripe_customer_id")
    if not sub_id and not cust:
        return False
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return True  # linkage exists but unverifiable → don't risk a real payer
    try:
        import stripe
        stripe.api_key = key
        if sub_id:
            s = stripe.Subscription.retrieve(sub_id)
            status = s.get("status") if isinstance(s, dict) else getattr(s, "status", None)
            return status in _ACTIVE_SUB_STATUSES
        subs = stripe.Subscription.list(customer=cust, status="all", limit=20)
        data = subs.get("data", []) if isinstance(subs, dict) else list(subs)
        for s in data:
            st = s.get("status") if isinstance(s, dict) else getattr(s, "status", None)
            if st in _ACTIVE_SUB_STATUSES:
                return True
        return False
    except Exception as e:
        _LOG.warning("Stripe verify failed (failing safe, not downgrading): %s", e)
        return True


def _notify_downgrade(uid: str, prev_plan: str):
    """In-app notification + optional Telegram nudge — keep Pro by upgrading."""
    label = prev_plan.title()
    try:
        from routes.notifications import push_notification
        push_notification(
            uid, "billing", "Your free period has ended",
            f"Your complimentary {label} access has ended, so your account is back on the "
            f"Free plan. Your credits are untouched. Upgrade any time to keep {label} features.",
        )
    except Exception:
        pass
    try:
        import asyncio
        from services.users import load_users
        u = next((x for x in load_users().get("users", []) if x.get("id") == uid), None)
        chat = u.get("telegram_id") if u else None
        if chat:
            from routes.telegram import tg_send
            asyncio.create_task(tg_send(
                chat,
                f"⏰ Your complimentary *{label}* period has ended — you're back on Free "
                f"(your credits are kept). Upgrade in the app to keep {label}."))
    except Exception:
        pass


def revert_one(uid: str, now: datetime) -> str:
    """Evaluate a single user. Returns one of:
    'skip' | 'still_valid' | 'reverted' | 'kept_paying' | 'cleared_stale'."""
    ustore = _load_user_store(uid)
    billing = ustore.get("billing") or {}
    if not billing.get("plan_expires"):
        return "skip"

    # Already free → just drop any stale markers so we stop re-checking.
    if (billing.get("plan") or "free") == "free":
        if billing.get("plan_expires") or billing.get("plan_extended_by"):
            billing.pop("plan_expires", None)
            billing.pop("plan_extended_by", None)
            ustore["billing"] = billing
            _save_user_store(uid, ustore)
            return "cleared_stale"
        return "skip"

    if not _is_expired(billing, now):
        return "still_valid"

    # Expired. ONLY ever touch complimentary grants — never a non-granted plan.
    if not _is_granted(billing):
        return "skip"

    # Belt-and-suspenders: a granted user who has since started paying is a real
    # subscriber now — leave their plan, just clear the stale grant markers.
    if _has_active_paid_subscription(billing):
        billing.pop("plan_expires", None)
        billing.pop("plan_extended_by", None)
        ustore["billing"] = billing
        _save_user_store(uid, ustore)
        return "kept_paying"

    # Pure expired grant, no active subscription → revert to free. Credits kept.
    prev = billing.get("plan", "free")
    billing["plan"] = "free"
    billing["plan_reverted_at"] = now.isoformat()
    billing["plan_reverted_from"] = prev
    billing.pop("plan_expires", None)
    billing.pop("plan_extended_by", None)
    ustore["billing"] = billing
    _save_user_store(uid, ustore)
    _notify_downgrade(uid, prev)
    return "reverted"


def revert_expired_plans(now: datetime = None) -> dict:
    """Scan all users and revert expired GRANTED plans to free. Returns a
    summary of counts. Never touches paying Stripe subscribers or credits."""
    now = now or datetime.utcnow()
    counts = {"reverted": 0, "kept_paying": 0, "cleared_stale": 0, "still_valid": 0}
    for uid in get_all_user_ids():
        try:
            outcome = revert_one(uid, now)
            if outcome in counts:
                counts[outcome] += 1
        except Exception as e:
            _LOG.warning("plan-expiry check failed for %s: %s", uid, e)
    if counts["reverted"] or counts["kept_paying"]:
        _LOG.info("plan expiry sweep: %s", counts)
    return counts
