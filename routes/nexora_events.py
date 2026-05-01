"""
Nexora Core Engine — Event Ingestion
POST /api/nexora/event  (protected by X-Nexora-Key header)
"""
import os
import json
from datetime import datetime
from fastapi import APIRouter, HTTPException, Request

router = APIRouter()

NEXORA_KEY = os.getenv("NEXORA_API_KEY", "nexora-core-2026")

ALLOWED_EVENTS = {"user.signup", "payment.received", "post.published", "error.critical"}

EVENTS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "nexora_events.json"
)


def _require_key(request: Request):
    key = request.headers.get("X-Nexora-Key", "")
    if key != NEXORA_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-Nexora-Key")


def _load_events() -> list:
    if os.path.exists(EVENTS_FILE):
        try:
            with open(EVENTS_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []


def _save_events(events: list):
    with open(EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2)


@router.post("/api/nexora/event")
async def nexora_event(request: Request):
    _require_key(request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event_type = body.get("event", "")
    if event_type not in ALLOWED_EVENTS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown event '{event_type}'. Allowed: {sorted(ALLOWED_EVENTS)}",
        )

    record = {
        "event":     event_type,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "platform":  body.get("platform", "ugoingviral"),
        "data":      body.get("data", {}),
    }

    events = _load_events()
    events.append(record)
    # Keep last 10 000 events on disk
    if len(events) > 10_000:
        events = events[-10_000:]
    _save_events(events)

    return {"ok": True, "event": event_type, "timestamp": record["timestamp"]}
