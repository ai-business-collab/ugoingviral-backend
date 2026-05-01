"""
Notification Center
GET  /api/notifications              — list notifications (auth)
POST /api/notifications/read/{id}    — mark one read (auth)
POST /api/notifications/read_all     — mark all read (auth)
GET  /api/notifications/prefs        — get preferences (auth)
POST /api/notifications/prefs        — save preferences (auth)

push_notification(uid, type, title, message) — importable helper for other routes
"""
import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from routes.auth import get_current_user
from services.store import store, save_store, _load_user_store, _save_user_store

router = APIRouter()

_TYPE_META = {
    "follower_milestone": {"icon": "🎉", "color": "#22c55e"},
    "credits_low":        {"icon": "⚠️",  "color": "#f59e0b"},
    "autopilot_posted":   {"icon": "🚀",  "color": "#00e5ff"},
    "referral_upgraded":  {"icon": "🎁",  "color": "#a78bfa"},
    "plan_upgraded":      {"icon": "💎",  "color": "#f59e0b"},
    "system":             {"icon": "ℹ️",  "color": "#6b7280"},
}


def push_notification(uid: str, notif_type: str, title: str, message: str):
    """Append a notification to a user's store. Safe to call from any route."""
    try:
        ustore = _load_user_store(uid)
        prefs = ustore.get("notification_prefs", {})
        if not prefs.get(notif_type, True):
            return
        notifs = ustore.setdefault("notifications", [])
        notifs.append({
            "id": str(uuid.uuid4())[:12],
            "type": notif_type,
            "title": title,
            "message": message,
            "read": False,
            "created_at": datetime.utcnow().isoformat() + "Z",
        })
        ustore["notifications"] = notifs[-200:]
        _save_user_store(uid, ustore)
    except Exception:
        pass


@router.get("/api/notifications")
def get_notifications(current_user: dict = Depends(get_current_user)):
    notifs = list(store.get("notifications", []))
    notifs_sorted = sorted(notifs, key=lambda n: n.get("created_at", ""), reverse=True)[:50]
    unread = sum(1 for n in notifs if not n.get("read"))
    for n in notifs_sorted:
        meta = _TYPE_META.get(n.get("type", "system"), _TYPE_META["system"])
        n["icon"] = meta["icon"]
        n["color"] = meta["color"]
    return {"notifications": notifs_sorted, "unread": unread}


@router.post("/api/notifications/read/{notif_id}")
def mark_read(notif_id: str, current_user: dict = Depends(get_current_user)):
    notifs = store.get("notifications", [])
    for n in notifs:
        if n.get("id") == notif_id:
            n["read"] = True
    store["notifications"] = notifs
    save_store()
    return {"ok": True, "unread": sum(1 for n in notifs if not n.get("read"))}


@router.post("/api/notifications/read_all")
def mark_all_read(current_user: dict = Depends(get_current_user)):
    notifs = store.get("notifications", [])
    for n in notifs:
        n["read"] = True
    store["notifications"] = notifs
    save_store()
    return {"ok": True, "unread": 0}


@router.get("/api/notifications/prefs")
def get_prefs(current_user: dict = Depends(get_current_user)):
    prefs = store.get("notification_prefs", {})
    return {
        "follower_milestone": prefs.get("follower_milestone", True),
        "credits_low":        prefs.get("credits_low", True),
        "autopilot_posted":   prefs.get("autopilot_posted", True),
        "referral_upgraded":  prefs.get("referral_upgraded", True),
    }


@router.post("/api/notifications/prefs")
async def save_prefs(request: Request, current_user: dict = Depends(get_current_user)):
    body = await request.json()
    prefs = {
        "follower_milestone": bool(body.get("follower_milestone", True)),
        "credits_low":        bool(body.get("credits_low", True)),
        "autopilot_posted":   bool(body.get("autopilot_posted", True)),
        "referral_upgraded":  bool(body.get("referral_upgraded", True)),
    }
    store["notification_prefs"] = prefs
    save_store()
    return {"ok": True}
