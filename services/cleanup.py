"""
Daily auto-cleanup of TEMPORARY data only — runs at 03:00 UTC.

What it removes (all transient):
  • failed video-render jobs older than 24h (in-memory queue)
  • completed render job records older than 7 days — these just cache the
    provider's temporary CDN URL, not the actual file

What it NEVER touches:
  • user uploaded files in user_content/
  • content-library items
  • generated videos saved to disk / persisted generated_videos records
  • anything the user paid credits for
"""
import asyncio
import logging
import os
import json
import shutil
from datetime import datetime, timedelta, timezone

_LOG = logging.getLogger("ugoingviral.cleanup")

CLEANUP_HOUR_UTC = 3

# ── Inactive-profile cleanup config (all overridable via .env) ────────────────
# A FREE profile with no activity for INACTIVE_CLEANUP_DAYS is deleted to free
# storage. Paying customers (or anyone who ever paid) and their content are
# NEVER touched. Users are warned by email INACTIVE_WARN_DAYS before deletion.
def _envint(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _envbool(name, default):
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

INACTIVE_CLEANUP_ENABLED = _envbool("INACTIVE_CLEANUP_ENABLED", True)
INACTIVE_CLEANUP_DAYS    = _envint("INACTIVE_CLEANUP_DAYS", 365)   # 12 months
INACTIVE_WARN_DAYS       = _envint("INACTIVE_WARN_DAYS", 14)       # email lead time
INACTIVE_CLEANUP_DRYRUN  = _envbool("INACTIVE_CLEANUP_DRYRUN", False)

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_LEARNINGS_FILE = os.path.join(_BASE, "anonymized_learnings.json")


def _parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_paying(billing: dict) -> bool:
    """True if this profile must NEVER be auto-deleted: any paid plan, a saved
    Stripe customer (ever paid), founding member, or a locked/active plan."""
    if not isinstance(billing, dict):
        return False
    plan = (billing.get("plan") or "free").lower()
    if plan and plan != "free":
        return True
    if billing.get("stripe_customer_id") or billing.get("stripe_subscription_id"):
        return True
    if billing.get("founding_member") or billing.get("ever_paid") or billing.get("locked_plan"):
        return True
    return False


def _append_learning(record: dict) -> None:
    """Append an ANONYMIZED learning record (no PII) so aggregate insights
    survive even after the profile is deleted."""
    try:
        data = []
        if os.path.exists(_LEARNINGS_FILE):
            with open(_LEARNINGS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        if not isinstance(data, list):
            data = []
        data.append(record)
        data = data[-50000:]
        with open(_LEARNINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as exc:
        _LOG.warning("could not append anonymized learning: %s", exc)


def _extract_learning(uid: str, ustore: dict, account_age_days: int) -> dict:
    """Pull non-identifying, aggregate learnings from a profile before deletion.
    Deliberately contains NO email, name, id or free-text content."""
    perf = [p for p in (ustore.get("content_performance") or []) if isinstance(p, dict)]
    ers = [p.get("engagement_rate", 0) for p in perf if p.get("engagement_rate") is not None]
    hours = {}
    for p in perf:
        ts = str(p.get("posted_at", ""))
        if len(ts) >= 13:
            try:
                h = int(ts[11:13])
                hours[h] = hours.get(h, 0) + 1
            except Exception:
                pass
    best_hour = max(hours, key=hours.get) if hours else None
    billing = ustore.get("billing") or {}
    return {
        "anonymized": True,
        "removed_at": datetime.now(timezone.utc).isoformat(),
        "niche": (ustore.get("niche") or (ustore.get("automation") or {}).get("niche") or "")[:40],
        "plan": (billing.get("plan") or "free"),
        "account_age_days": account_age_days,
        "content_pieces": len(ustore.get("content_history") or []),
        "posts_tracked": len(perf),
        "avg_engagement_rate": round(sum(ers) / len(ers), 4) if ers else None,
        "best_posting_hour": best_hour,
        "platforms_connected": len([p for p, v in (ustore.get("connections") or {}).items()
                                    if isinstance(v, dict) and v.get("username")]),
    }


def _send_inactivity_warning(email: str, name: str, delete_on: datetime) -> None:
    try:
        from routes.email import send_system_email
        if not email or email.startswith("qa-"):
            return
        first = (name or email.split("@")[0]).split(" ")[0].capitalize()
        when = delete_on.strftime("%d %B %Y")
        months = max(1, INACTIVE_CLEANUP_DAYS // 30)
        html = f"""<!DOCTYPE html><html><body style="font-family:Arial,sans-serif;background:#080c18;color:#a0b0c0;padding:24px">
<div style="max-width:560px;margin:0 auto;background:#0d1526;border:1px solid rgba(255,255,255,.07);border-radius:16px;padding:28px 32px">
<div style="font-size:20px;font-weight:800;color:#f0f4f8">Ugoin<span style="color:#00e5ff">g</span>Viral</div>
<h2 style="color:#f0f4f8">We miss you, {first} 👋</h2>
<p>Your free UgoingViral account has been inactive for a while. To save storage we
remove free accounts that stay inactive for {months} months.</p>
<p><strong style="color:#f0f4f8">Just log in before {when}</strong> to keep your account and all your content.
If you don't, your free account will be removed on that date.</p>
<p style="margin-top:18px"><a href="https://ugoingviral.com/app" style="background:#00e5ff;color:#001018;padding:12px 22px;border-radius:10px;text-decoration:none;font-weight:700">Keep my account →</a></p>
<p style="font-size:12px;color:#6b7d8f;margin-top:20px">Paid plans are never auto-removed. Questions? support@ugoingviral.com</p>
</div></body></html>"""
        send_system_email(email, "Your UgoingViral account will be removed soon", html)
    except Exception as exc:
        _LOG.warning("inactivity warning email failed: %s", exc)


def _delete_profile_fully(uid: str) -> None:
    """Remove a profile from the user store and delete ALL its on-disk media."""
    from services.users import load_users, save_users
    from services.store import USER_DATA_DIR
    try:
        data = load_users()
        data["users"] = [u for u in data.get("users", []) if u.get("id") != uid]
        save_users(data)
    except Exception as exc:
        _LOG.warning("delete profile %s: users store: %s", uid, exc)
    for path in (
        os.path.join(USER_DATA_DIR, f"{uid}.json"),
        os.path.join(_BASE, "user_content", uid),
        os.path.join(_BASE, "uploads", "user_media", uid),
    ):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            elif os.path.exists(path):
                os.remove(path)
        except Exception as exc:
            _LOG.warning("delete profile %s: path %s: %s", uid, path, exc)


def cleanup_inactive_profiles() -> dict:
    """Warn, then delete, FREE profiles inactive for INACTIVE_CLEANUP_DAYS.
    Never touches paying customers or their content. Returns a summary."""
    summary = {"enabled": INACTIVE_CLEANUP_ENABLED, "dry_run": INACTIVE_CLEANUP_DRYRUN,
               "scanned": 0, "warned": 0, "deleted": 0, "skipped_paid": 0,
               "days": INACTIVE_CLEANUP_DAYS, "deleted_ids": []}
    if not INACTIVE_CLEANUP_ENABLED:
        return summary
    try:
        from services.users import load_users
        from services.store import _load_user_store, _save_user_store
    except Exception as exc:
        _LOG.error("inactive cleanup imports failed: %s", exc)
        return summary

    now = datetime.now(timezone.utc)
    warn_threshold = INACTIVE_CLEANUP_DAYS - INACTIVE_WARN_DAYS
    users = load_users().get("users", [])
    summary["scanned"] = len(users)

    for u in users:
        uid = u.get("id")
        if not uid:
            continue
        ustore = _load_user_store(uid)
        billing = ustore.get("billing") or {}

        # Hard guard: paying customers are never auto-removed.
        if _is_paying(billing):
            summary["skipped_paid"] += 1
            continue

        last_active = _parse_iso(u.get("last_login")) or _parse_iso(u.get("created_at"))
        if not last_active:
            continue  # unknown age → safe, never delete
        days_inactive = (now - last_active).days
        created = _parse_iso(u.get("created_at")) or last_active
        account_age_days = (now - created).days
        warned_at = _parse_iso(billing.get("deletion_warned_at"))

        # ── Warning phase ──
        if warn_threshold <= days_inactive < INACTIVE_CLEANUP_DAYS and not warned_at:
            if not INACTIVE_CLEANUP_DRYRUN:
                billing["deletion_warned_at"] = now.isoformat()
                ustore["billing"] = billing
                _save_user_store(uid, ustore)
                _send_inactivity_warning(u.get("email", ""), u.get("name", ""),
                                         now + timedelta(days=INACTIVE_WARN_DAYS))
            summary["warned"] += 1
            continue

        # ── Deletion phase — only after a warning + grace period ──
        if days_inactive >= INACTIVE_CLEANUP_DAYS:
            if not warned_at or (now - warned_at).days < INACTIVE_WARN_DAYS:
                # Crossed the line without a long-enough warning → warn now,
                # delete on a later run once the grace period has passed.
                if not warned_at and not INACTIVE_CLEANUP_DRYRUN:
                    billing["deletion_warned_at"] = now.isoformat()
                    ustore["billing"] = billing
                    _save_user_store(uid, ustore)
                    _send_inactivity_warning(u.get("email", ""), u.get("name", ""),
                                             now + timedelta(days=INACTIVE_WARN_DAYS))
                    summary["warned"] += 1
                continue
            # Keep anonymized learnings, then delete.
            _append_learning(_extract_learning(uid, ustore, account_age_days))
            if not INACTIVE_CLEANUP_DRYRUN:
                _delete_profile_fully(uid)
            summary["deleted"] += 1
            summary["deleted_ids"].append(uid)

    return summary


def _seconds_until(hour_utc: int) -> float:
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if nxt <= now:
        nxt += timedelta(days=1)
    return (nxt - now).total_seconds()


def run_cleanup_once() -> dict:
    """Execute one cleanup pass. Safe to call manually. Returns a summary."""
    from routes.studio import cleanup_temporary_data
    result = cleanup_temporary_data(failed_max_age_h=24, url_cache_max_age_days=7)
    # Also warn/delete long-inactive FREE profiles (paying customers untouched).
    try:
        result = dict(result) if isinstance(result, dict) else {"temporary": result}
        result["inactive_profiles"] = cleanup_inactive_profiles()
    except Exception as exc:
        _LOG.error("inactive-profile cleanup failed: %s", exc)
    return result


async def run_daily_cleanup():
    """Background task: sleep until 03:00 UTC, run cleanup, repeat every 24h."""
    await asyncio.sleep(30)  # let the app finish starting up
    while True:
        await asyncio.sleep(_seconds_until(CLEANUP_HOUR_UTC))
        try:
            result = run_cleanup_once()
            _LOG.info("daily cleanup at 03:00 UTC: %s", result)
        except Exception as exc:
            _LOG.error("daily cleanup failed: %s", exc)
        # Avoid re-firing within the same minute before the clock advances.
        await asyncio.sleep(60)
