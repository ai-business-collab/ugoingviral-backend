import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Request as _SubReq
from pydantic import BaseModel
from routes.auth import get_current_user
from services.store import _load_user_store, _save_user_store
from routes.billing import PLANS

router = APIRouter()

AVATAR_COLORS = ["#00e5ff","#7c3aed","#10b981","#f59e0b","#ef4444","#ec4899","#3b82f6","#84cc16"]


def _sub_store_id(user_id: str, sub_id: str) -> str:
    return f"{user_id}_sub_{sub_id}"


def _get_sub_accounts(user_id: str) -> list:
    ustore = _load_user_store(user_id)
    return ustore.get("sub_accounts", [])


def _plan_limit(user_id: str) -> int:
    ustore = _load_user_store(user_id)
    billing = ustore.get("billing", {})
    plan_key = billing.get("plan", "free")
    base = PLANS.get(plan_key, PLANS["free"]).get("sub_accounts", 0)
    extra = int(billing.get("extra_sub_slots", 0))
    return base + extra


def resolve_store_id(current_user: dict, sub_id: str | None) -> str:
    """Return effective user/sub store ID. Validates ownership."""
    user_id = current_user["id"]
    if not sub_id:
        return user_id
    subs = _get_sub_accounts(user_id)
    if not any(s["id"] == sub_id for s in subs):
        raise HTTPException(status_code=403, detail="Sub-konto ikke fundet")
    return _sub_store_id(user_id, sub_id)


class SubAccountCreate(BaseModel):
    name: str
    platform_focus: str = ""
    avatar_color: str = ""
    niche: str = ""


class SubAccountUpdate(BaseModel):
    name: str = ""
    platform_focus: str = ""
    avatar_color: str = ""


@router.get("/api/subaccounts")
def list_subaccounts(current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    limit = _plan_limit(user_id)
    subs = _get_sub_accounts(user_id)
    return {
        "sub_accounts": subs,
        "limit": limit,
        "count": len(subs),
    }


@router.post("/api/subaccounts")
def create_subaccount(req: SubAccountCreate, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    ustore_check = _load_user_store(user_id)
    if ustore_check.get("billing", {}).get("subs_blocked"):
        raise HTTPException(status_code=403, detail="Oprettelse af under-konti er blokeret af administrator.")
    limit = _plan_limit(user_id)
    subs = _get_sub_accounts(user_id)

    if len(subs) >= limit:
        raise HTTPException(
            status_code=403,
            detail=f"Din plan tillader maks {limit} under-konti. Opgrader for at få flere."
        )

    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Navn er påkrævet")

    color = req.avatar_color if req.avatar_color else AVATAR_COLORS[len(subs) % len(AVATAR_COLORS)]
    sub = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "platform_focus": req.platform_focus.strip(),
        "avatar_color": color,
        "niche": req.niche.strip(),
        "created_at": datetime.utcnow().isoformat(),
    }

    ustore = _load_user_store(user_id)
    ustore.setdefault("sub_accounts", []).append(sub)
    _save_user_store(user_id, ustore)

    # Initialize empty store for sub-account
    from services.store import DEFAULT_STORE
    import copy
    sub_store = copy.deepcopy(DEFAULT_STORE)
    sub_store["billing"] = {"plan": "sub", "parent_user_id": user_id}
    _save_user_store(_sub_store_id(user_id, sub["id"]), sub_store)

    return sub


@router.patch("/api/subaccounts/{sub_id}")
def update_subaccount(sub_id: str, req: SubAccountUpdate, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    ustore = _load_user_store(user_id)
    subs = ustore.get("sub_accounts", [])
    sub = next((s for s in subs if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Sub-konto ikke fundet")

    if req.name.strip():
        sub["name"] = req.name.strip()
    if req.platform_focus is not None:
        sub["platform_focus"] = req.platform_focus.strip()
    if req.avatar_color:
        sub["avatar_color"] = req.avatar_color

    _save_user_store(user_id, ustore)
    return sub


@router.delete("/api/subaccounts/{sub_id}")
def delete_subaccount(sub_id: str, current_user: dict = Depends(get_current_user)):
    user_id = current_user["id"]
    ustore = _load_user_store(user_id)
    subs = ustore.get("sub_accounts", [])
    before = len(subs)
    ustore["sub_accounts"] = [s for s in subs if s["id"] != sub_id]
    if len(ustore.get("sub_accounts", {})) == before:
        raise HTTPException(status_code=404, detail="Sub-konto ikke fundet")
    _save_user_store(user_id, ustore)

    # Remove sub store file
    import os
    from services.store import USER_DATA_DIR
    sub_path = os.path.join(USER_DATA_DIR, f"{_sub_store_id(user_id, sub_id)}.json")
    try:
        if os.path.exists(sub_path):
            os.remove(sub_path)
    except Exception:
        pass

    return {"ok": True}


@router.patch("/api/subaccounts/{sub_id}/status")
async def set_subaccount_status(sub_id: str, req_obj: _SubReq, current_user: dict = Depends(get_current_user)):
    """Deaktivér eller reaktivér en under-konto uden at slette data."""
    body = await req_obj.json()
    status = body.get("status", "active")
    if status not in ("active", "inactive"):
        raise HTTPException(status_code=400, detail="Status skal være 'active' eller 'inactive'")
    user_id = current_user["id"]
    ustore = _load_user_store(user_id)
    subs = ustore.get("sub_accounts", [])
    sub = next((s for s in subs if s["id"] == sub_id), None)
    if not sub:
        raise HTTPException(status_code=404, detail="Sub-konto ikke fundet")
    sub["status"] = status
    _save_user_store(user_id, ustore)
    return sub


@router.get("/api/subaccounts/{sub_id}/summary")
def subaccount_summary(sub_id: str, current_user: dict = Depends(get_current_user)):
    """Quick summary stats for a sub-account."""
    store_id = resolve_store_id(current_user, sub_id)
    sub_store = _load_user_store(store_id)

    connections = sub_store.get("connections", {})
    connected = [p for p, v in connections.items() if isinstance(v, dict) and v.get("username")]
    content_count = len(sub_store.get("content_history", []))
    scheduled = len([p for p in sub_store.get("scheduled_posts", []) if p.get("status") == "scheduled"])

    return {
        "connected_platforms": connected,
        "content_count": content_count,
        "scheduled_posts": scheduled,
    }
