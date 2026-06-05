"""
Leads / CRM pipeline — UgoingViral
==================================
A lightweight per-user sales pipeline (Kanban) for tracking outreach leads
through stages: new → contacted → replied → interested → closed / lost.

Leads are stored in the per-user store under `store["leads"]`. Auth is enforced
via get_current_user; the store proxy is already bound to the same user by the
auth middleware.
"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Depends, HTTPException
from services.store import store, save_store
from routes.auth import get_current_user

router = APIRouter()

# Canonical pipeline stages (keys) + their display labels.
STAGES = ["new", "contacted", "replied", "interested", "closed", "lost"]
STAGE_LABELS = {
    "new":        "New Lead",
    "contacted":  "Contacted",
    "replied":    "Replied",
    "interested": "Interested",
    "closed":     "Closed",
    "lost":       "Lost",
}

_FIELDS = ("name", "handle", "platform", "note", "value", "stage")


def _now() -> str:
    return datetime.utcnow().isoformat()


def _clean_stage(value, default="new") -> str:
    return value if value in STAGES else default


@router.get("/api/leads")
def list_leads(current_user: dict = Depends(get_current_user)):
    """Return all leads for the current user plus the stage definitions."""
    leads = store.get("leads", []) or []
    counts = {s: 0 for s in STAGES}
    for l in leads:
        s = _clean_stage(l.get("stage"))
        counts[s] += 1
    return {
        "leads": leads,
        "stages": [{"key": s, "label": STAGE_LABELS[s]} for s in STAGES],
        "counts": counts,
    }


@router.post("/api/leads")
async def create_lead(request: Request, current_user: dict = Depends(get_current_user)):
    """Create a new lead. Only `name` is required."""
    d = await request.json()
    lead = {
        "id":         uuid.uuid4().hex[:12],
        "name":       (d.get("name") or "").strip() or "Untitled lead",
        "handle":     (d.get("handle") or "").strip(),
        "platform":   (d.get("platform") or "").strip(),
        "note":       (d.get("note") or "").strip(),
        "value":      (str(d.get("value")).strip() if d.get("value") not in (None, "") else ""),
        "stage":      _clean_stage(d.get("stage")),
        "created_at": _now(),
        "updated_at": _now(),
    }
    leads = store.get("leads", []) or []
    leads.insert(0, lead)
    store["leads"] = leads
    save_store()
    return lead


@router.patch("/api/leads/{lead_id}")
async def update_lead(lead_id: str, request: Request, current_user: dict = Depends(get_current_user)):
    """Update a lead's fields (e.g. move it to another stage via drag-and-drop)."""
    d = await request.json()
    leads = store.get("leads", []) or []
    for l in leads:
        if l.get("id") == lead_id:
            for k in _FIELDS:
                if k in d and d[k] is not None:
                    if k == "stage":
                        l[k] = _clean_stage(d[k], l.get("stage", "new"))
                    else:
                        l[k] = (str(d[k]).strip() if k in ("value",) else d[k])
            l["updated_at"] = _now()
            store["leads"] = leads
            save_store()
            return l
    raise HTTPException(status_code=404, detail="Lead not found")


@router.delete("/api/leads/{lead_id}")
def delete_lead(lead_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a lead."""
    leads = store.get("leads", []) or []
    new_leads = [l for l in leads if l.get("id") != lead_id]
    if len(new_leads) == len(leads):
        raise HTTPException(status_code=404, detail="Lead not found")
    store["leads"] = new_leads
    save_store()
    return {"ok": True, "deleted": lead_id}
