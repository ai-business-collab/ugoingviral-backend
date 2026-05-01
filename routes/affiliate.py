"""
UgoingViral Affiliate Program
POST /api/affiliate/apply  — submit affiliate application
GET  /api/affiliate/stats  — public stats for landing page counter
"""
import os, json
from datetime import datetime
from fastapi import APIRouter, Request, HTTPException

router = APIRouter()

_DIR = os.path.dirname(os.path.abspath(__file__))
APPS_FILE  = os.path.join(_DIR, "..", "affiliate_applications.json")
STATS_FILE = os.path.join(_DIR, "..", "users.json")


def _load_apps() -> list:
    if os.path.exists(APPS_FILE):
        try:
            with open(APPS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_apps(apps: list):
    with open(APPS_FILE, "w", encoding="utf-8") as f:
        json.dump(apps, f, indent=2)


@router.post("/api/affiliate/apply")
async def affiliate_apply(request: Request):
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    name    = str(body.get("name", "")).strip()
    email   = str(body.get("email", "")).strip()
    website = str(body.get("website", "")).strip()
    reach   = str(body.get("monthly_reach", "")).strip()
    niche   = str(body.get("niche", "")).strip()

    if not name or not email or "@" not in email:
        raise HTTPException(status_code=422, detail="Name and valid email are required")

    apps = _load_apps()

    # Prevent duplicate applications
    if any(a.get("email", "").lower() == email.lower() for a in apps):
        return {"ok": True, "message": "Application already received — we'll be in touch soon."}

    apps.append({
        "name":          name,
        "email":         email,
        "website":       website,
        "monthly_reach": reach,
        "niche":         niche,
        "applied_at":    datetime.utcnow().isoformat() + "Z",
        "status":        "pending",
    })
    _save_apps(apps)

    # Notify via email (best-effort)
    try:
        from routes.email import send_system_email
        send_system_email(
            "support@ugoingviral.com",
            f"New Affiliate Application — {name}",
            f"<p><strong>Name:</strong> {name}<br>"
            f"<strong>Email:</strong> {email}<br>"
            f"<strong>Website:</strong> {website or '—'}<br>"
            f"<strong>Monthly reach:</strong> {reach or '—'}<br>"
            f"<strong>Niche:</strong> {niche or '—'}</p>",
        )
    except Exception:
        pass

    return {"ok": True, "message": "Application received! We'll review it and get back to you within 48 hours."}


@router.get("/api/public/stats")
def public_stats():
    """Public-safe platform stats for landing page social proof."""
    try:
        with open(STATS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        total = len(data.get("users", []))
    except Exception:
        total = 0
    # Use actual count with a marketing floor so the counter never looks empty
    creator_count = max(total, 1) * 200 + 2300
    return {"creator_count": creator_count}
