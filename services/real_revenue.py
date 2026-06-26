"""
Real revenue source of truth — Stripe.

Nex and the admin dashboard must never report fabricated revenue. The only
honest source of recurring revenue is Stripe's actual *active* subscriptions
(what customers are really being billed). Locally-assigned plans (admin comps,
test/founder accounts, manual grants) are NOT revenue and must not be counted.

`real_mrr()` returns the true monthly recurring revenue computed from Stripe
active subscriptions, normalising yearly plans to a monthly figure. If Stripe
is not configured or the call fails, it returns 0 with a clear flag — never a
guess. Results are cached briefly so the 5-minute Nex poll and the admin
dashboard don't hammer the Stripe API.
"""
import os
import time
import logging

_LOGGER = logging.getLogger(__name__)

_CACHE: dict = {}
_CACHE_TTL = 900  # 15 minutes


def _compute() -> dict:
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    if not key:
        return {
            "mrr": 0, "arr": 0, "currency": None,
            "active_subscriptions": 0,
            "stripe_configured": False,
            "source": "stripe",
            "note": "Stripe not configured — no revenue data",
        }
    try:
        import stripe
        stripe.api_key = key
        mrr = 0.0
        count = 0
        currencies = set()
        subs = stripe.Subscription.list(status="active", limit=100)
        for s in subs.auto_paging_iter():
            count += 1
            for item in s["items"]["data"]:
                price = item.get("price") or {}
                unit = (price.get("unit_amount") or 0) / 100.0
                qty = item.get("quantity", 1) or 1
                rec = price.get("recurring") or {}
                interval = rec.get("interval", "month")
                interval_count = rec.get("interval_count", 1) or 1
                # Normalise everything to a monthly figure.
                months = {"day": 1 / 30, "week": 1 / 4.345,
                          "month": 1, "year": 12}.get(interval, 1) * interval_count
                if months <= 0:
                    months = 1
                mrr += (unit * qty) / months
                cur = price.get("currency")
                if cur:
                    currencies.add(cur.upper())
        mrr = round(mrr, 2)
        return {
            "mrr": mrr,
            "arr": round(mrr * 12, 2),
            "currency": (sorted(currencies)[0] if currencies else None),
            "active_subscriptions": count,
            "stripe_configured": True,
            "source": "stripe",
        }
    except Exception as exc:
        _LOGGER.error("Stripe MRR query failed: %s", exc)
        return {
            "mrr": 0, "arr": 0, "currency": None,
            "active_subscriptions": 0,
            "stripe_configured": True,
            "source": "stripe",
            "error": str(exc)[:160],
            "note": "Stripe query failed — showing 0 instead of a guess",
        }


def real_mrr(force: bool = False) -> dict:
    """Return real Stripe-based MRR/ARR, cached for 15 minutes."""
    now = time.time()
    cached = _CACHE.get("data")
    if not force and cached and (now - _CACHE.get("ts", 0)) < _CACHE_TTL:
        return cached
    data = _compute()
    _CACHE["data"] = data
    _CACHE["ts"] = now
    return data
