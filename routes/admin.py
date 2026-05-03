import os
import json
import uuid
from datetime import datetime, timedelta
from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from pydantic import BaseModel
from typing import Optional
from passlib.context import CryptContext
from services.users import load_users
from services.store import _load_user_store, _save_user_store, USER_DATA_DIR

router = APIRouter()
security = HTTPBearer(auto_error=False)
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

PLANS = {
    "free":     {"name": "Free",     "price": 0,   "credits": 10},
    "starter":  {"name": "Starter",  "price": 40,  "credits": 200},
    "growth":   {"name": "Growth",   "price": 57,  "credits": 500},
    "basic":    {"name": "Basic",    "price": 70,  "credits": 1000},
    "pro":      {"name": "Pro",      "price": 140, "credits": 2000},
    "elite":    {"name": "Elite",    "price": 210, "credits": 5000},
    "personal": {"name": "Personal", "price": 350, "credits": 999999},
    "agency":   {"name": "Agency",   "price": 199, "credits": 2000},
}

ADMIN_EMAIL = "admin@ugoingviral.com"
ADMIN_NAME  = "Michael"

BASE = os.path.dirname(os.path.abspath(__file__))
EMPLOYEES_FILE  = os.path.join(BASE, "..", "admin_users.json")
ASSIGNMENTS_FILE = os.path.join(BASE, "..", "customer_assignments.json")
GUIDELINES_FILE = os.path.join(BASE, "..", "assistant_guidelines.json")


# ── Config helpers ────────────────────────────────────────────────────────────

def _read_env(key: str, default: str = "") -> str:
    env_path = os.path.join(BASE, "..", ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{key}="):
                    val = line.split("=", 1)[1].strip()
                    if val:
                        return val
    return os.getenv(key, default)


def _admin_password() -> str:
    return _read_env("ADMIN_PASSWORD", "***REDACTED-ADMIN-PASSWORD***")


def _jwt_secret() -> str:
    s = _read_env("ADMIN_JWT_SECRET", "")
    return s or _read_env("ADMIN_PASSWORD", "ugv-admin-jwt-2026")


# ── File helpers ──────────────────────────────────────────────────────────────

def _load_employees() -> dict:
    if not os.path.exists(EMPLOYEES_FILE):
        return {"employees": []}
    with open(EMPLOYEES_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_employees(data: dict):
    with open(EMPLOYEES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_assignments() -> dict:
    if not os.path.exists(ASSIGNMENTS_FILE):
        return {"assignments": {}}
    with open(ASSIGNMENTS_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_assignments(data: dict):
    with open(ASSIGNMENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _load_guidelines() -> dict:
    default = {
        "title": "Personal Assistant Retningslinjer",
        "updated_at": "",
        "sections": [
            {"heading": "Første kontakt",
             "text": "Tag kontakt inden for 24 timer efter kunden er tildelt. Præsentér dig selv og spørg til deres mål med platformen."},
            {"heading": "Onboarding skema",
             "text": "1. Bekræft konti og platforme er tilkoblet\n2. Gennemgå produkter / influencer profil\n3. Opsæt auto-posting tidsplan\n4. Sæt content-kalender for første måned"},
            {"heading": "Ugentlig opfølgning",
             "text": "Tjek metrics hver mandag. Notér resultater i support noter. Juster strategi ved lav engagement."},
            {"heading": "Eskalering til owner",
             "text": "Tekniske fejl, klager, refund-anmodninger eller ønske om plan-ændring eskaleres til Michael."},
        ],
    }
    if not os.path.exists(GUIDELINES_FILE):
        return default
    with open(GUIDELINES_FILE, encoding="utf-8") as f:
        return json.load(f)


def _save_guidelines(data: dict):
    data["updated_at"] = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
    with open(GUIDELINES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


# ── Seed Louise ved opstart ───────────────────────────────────────────────────

def _seed_louise():
    data = _load_employees()
    if any(e["email"].lower() == "louise@ugoingviral.com" for e in data["employees"]):
        return
    louise = {
        "id": str(uuid.uuid4()),
        "name": "Louise",
        "email": "louise@ugoingviral.com",
        "title": "Sales & Customer Success Manager",
        "role": "support",
        "hashed_password": pwd_context.hash("ugoingviral2026"),
        "profile_image": "",
        "created_at": datetime.utcnow().isoformat()[:10],
    }
    data["employees"].append(louise)
    _save_employees(data)


_seed_louise()


# ── Auth ──────────────────────────────────────────────────────────────────────

def _decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, _jwt_secret(), algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=401, detail="Ugyldig eller udløbet token")


def require_owner(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401)
    payload = _decode_token(credentials.credentials)
    if payload.get("role") != "owner":
        raise HTTPException(status_code=403, detail="Kun owner har adgang")
    return payload


def require_support_or_above(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401)
    payload = _decode_token(credentials.credentials)
    if payload.get("role") not in ("owner", "support"):
        raise HTTPException(status_code=403, detail="Kræver minimum support-adgang")
    return payload


def require_staff(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401)
    payload = _decode_token(credentials.credentials)
    if payload.get("role") not in ("owner", "support", "employee"):
        raise HTTPException(status_code=401)
    return payload


# keep backward compat
def get_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401)
    payload = _decode_token(credentials.credentials)
    if payload.get("role") not in ("owner", "admin"):
        raise HTTPException(status_code=401)
    return payload


# ── Models ────────────────────────────────────────────────────────────────────

class AdminLoginRequest(BaseModel):
    email: str
    password: str


class CreateEmployeeRequest(BaseModel):
    name: str
    email: str
    title: str = ""
    password: str
    role: str = "employee"


class UpdateEmployeeProfileRequest(BaseModel):
    name: Optional[str] = None
    title: Optional[str] = None
    profile_image: Optional[str] = None


class AssignRequest(BaseModel):
    user_id: str
    employee_id: str


class SetPlanRequest(BaseModel):
    plan: str


class AdjustCreditsRequest(BaseModel):
    credits: int


class SupportNoteRequest(BaseModel):
    user_id: str
    text: str


class ResolveNoteRequest(BaseModel):
    user_id: str
    note_id: str


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class GuidelinesRequest(BaseModel):
    title: Optional[str] = None
    sections: Optional[list] = None


# ── Unified login ─────────────────────────────────────────────────────────────

@router.post("/api/admin/login")
def admin_login(req: AdminLoginRequest):
    secret = _jwt_secret()

    # Owner check
    if req.email.strip().lower() == ADMIN_EMAIL.lower():
        if req.password != _admin_password():
            raise HTTPException(status_code=401, detail="Forkert adgangskode")
        token = jwt.encode(
            {"role": "owner", "name": ADMIN_NAME,
             "exp": datetime.utcnow() + timedelta(hours=12)},
            secret, algorithm="HS256",
        )
        return {"token": token, "role": "owner", "name": ADMIN_NAME}

    # Employee / support check
    data = _load_employees()
    emp = next((e for e in data["employees"] if e["email"].lower() == req.email.strip().lower()), None)
    if not emp or not pwd_context.verify(req.password, emp["hashed_password"]):
        raise HTTPException(status_code=401, detail="Forkert email eller adgangskode")

    emp_role = emp.get("role", "employee")
    token = jwt.encode(
        {
            "role": emp_role,
            "employee_id": emp["id"],
            "name": emp["name"],
            "email": emp["email"],
            "title": emp.get("title", ""),
            "profile_image": emp.get("profile_image", ""),
            "exp": datetime.utcnow() + timedelta(hours=12),
        },
        secret, algorithm="HS256",
    )
    return {
        "token": token,
        "role": emp_role,
        "name": emp["name"],
        "title": emp.get("title", ""),
        "profile_image": emp.get("profile_image", ""),
    }


# Backward-compat endpoint (bruges ikke længere af frontend)
@router.post("/api/admin/employee_login")
def employee_login_legacy(req: AdminLoginRequest):
    return admin_login(req)


# ── Owner/Support: Users ──────────────────────────────────────────────────────

def _build_user_list(assignments: dict):
    users_data = load_users()
    employees_data = _load_employees()
    emp_map = {e["id"]: e for e in employees_data.get("employees", [])}
    result = []
    for user in users_data.get("users", []):
        uid = user["id"]
        user_store = _load_user_store(uid)
        billing = user_store.get("billing", {"plan": "free", "credits": 50})
        plan_key = billing.get("plan", "free")
        plan_info = PLANS.get(plan_key, PLANS["free"])
        data_path = os.path.join(USER_DATA_DIR, f"{uid}.json")
        last_active = "–"
        if os.path.exists(data_path):
            mtime = os.path.getmtime(data_path)
            last_active = datetime.fromtimestamp(mtime).strftime("%d/%m %H:%M")
        assigned_id = assignments.get("assignments", {}).get(uid)
        assigned_emp = emp_map.get(assigned_id)
        settings = user_store.get("settings", {})
        api_enabled = settings.get("api_enabled", {})
        connected = [p for p, v in api_enabled.items() if v]
        notes = user_store.get("support_notes", [])
        open_notes = sum(1 for n in notes if not n.get("resolved"))
        status = user_store.get("status", "active")
        sub_accounts = user_store.get("sub_accounts", [])
        billing_info = user_store.get("billing", {})
        extra_sub_slots = billing_info.get("extra_sub_slots", 0)
        subs_blocked = billing_info.get("subs_blocked", False)
        result.append({
            "id": uid,
            "email": user["email"],
            "name": user.get("name", ""),
            "company": user.get("company", ""),
            "created_at": user.get("created_at", "")[:10],
            "plan": plan_key,
            "plan_name": plan_info["name"],
            "credits": billing.get("credits", plan_info["credits"]),
            "max_credits": plan_info["credits"],
            "last_active": last_active,
            "connected_platforms": connected,
            "assigned_employee_id": assigned_id or "",
            "assigned_employee_name": assigned_emp["name"] if assigned_emp else "",
            "open_notes": open_notes,
            "status": status,
            "sub_count": len(sub_accounts),
            "sub_accounts": [{"id": s["id"], "name": s["name"], "status": s.get("status","active"), "created_at": s.get("created_at","")[:10]} for s in sub_accounts],
            "extra_sub_slots": extra_sub_slots,
            "subs_blocked": subs_blocked,
        })
    return result


@router.get("/api/admin/users")
def admin_get_users(staff=Depends(require_support_or_above)):
    assignments = _load_assignments()
    return _build_user_list(assignments)


def _auto_assign_personal(user_id: str):
    """Tildel automatisk til mindst belastede medarbejder og opret system-note."""
    data = _load_employees()
    employees = data.get("employees", [])
    if not employees:
        return
    assignments = _load_assignments()
    if assignments.get("assignments", {}).get(user_id):
        return
    counts = {e["id"]: 0 for e in employees}
    for uid, eid in assignments.get("assignments", {}).items():
        if eid in counts:
            counts[eid] += 1
    least_loaded_id = min(counts, key=counts.get)
    assignments["assignments"][user_id] = least_loaded_id
    _save_assignments(assignments)

    assigned_emp = next((e for e in employees if e["id"] == least_loaded_id), None)
    emp_name = assigned_emp["name"] if assigned_emp else "en medarbejder"
    user_store = _load_user_store(user_id)
    note = {
        "id": str(uuid.uuid4())[:8],
        "text": f"Auto-tildelt til {emp_name} som personlig assistent. Tag kontakt til kunden inden for 24 timer.",
        "author": "System",
        "created_at": datetime.utcnow().strftime("%d/%m %H:%M"),
        "resolved": False,
        "system": True,
    }
    user_store.setdefault("support_notes", []).insert(0, note)
    _save_user_store(user_id, user_store)


@router.post("/api/admin/users/{user_id}/plan")
def admin_set_plan(user_id: str, req: SetPlanRequest, admin=Depends(require_owner)):
    if req.plan not in PLANS:
        raise HTTPException(status_code=400, detail="Ukendt plan")
    user_store = _load_user_store(user_id)
    plan_info = PLANS[req.plan]
    user_store["billing"] = {"plan": req.plan, "credits": plan_info["credits"]}
    _save_user_store(user_id, user_store)
    if req.plan == "personal":
        _auto_assign_personal(user_id)
    return {"ok": True, "plan": req.plan, "credits": plan_info["credits"]}


@router.post("/api/admin/users/{user_id}/credits")
def admin_set_credits(user_id: str, req: AdjustCreditsRequest, admin=Depends(require_owner)):
    user_store = _load_user_store(user_id)
    billing = user_store.get("billing", {"plan": "starter", "credits": 300})
    billing["credits"] = max(0, req.credits)
    user_store["billing"] = billing
    _save_user_store(user_id, user_store)
    return {"ok": True, "credits": billing["credits"]}


# ── Owner: Employees ──────────────────────────────────────────────────────────

@router.get("/api/admin/employees")
def list_employees(admin=Depends(require_owner)):
    data = _load_employees()
    return [
        {
            "id": e["id"],
            "name": e["name"],
            "email": e["email"],
            "title": e.get("title", ""),
            "role": e.get("role", "employee"),
            "profile_image": e.get("profile_image", ""),
            "created_at": e.get("created_at", ""),
        }
        for e in data.get("employees", [])
    ]


@router.post("/api/admin/employees")
def create_employee(req: CreateEmployeeRequest, admin=Depends(require_owner)):
    if req.role not in ("employee", "support"):
        raise HTTPException(status_code=400, detail="Rolle skal være 'employee' eller 'support'")
    data = _load_employees()
    if any(e["email"].lower() == req.email.lower() for e in data["employees"]):
        raise HTTPException(status_code=400, detail="Email allerede i brug")
    emp = {
        "id": str(uuid.uuid4()),
        "name": req.name.strip(),
        "email": req.email.strip().lower(),
        "title": req.title.strip(),
        "role": req.role,
        "hashed_password": pwd_context.hash(req.password),
        "profile_image": "",
        "created_at": datetime.utcnow().isoformat()[:10],
    }
    data["employees"].append(emp)
    _save_employees(data)
    return {"id": emp["id"], "name": emp["name"], "email": emp["email"],
            "title": emp["title"], "role": emp["role"]}


@router.delete("/api/admin/employees/{employee_id}")
def delete_employee(employee_id: str, admin=Depends(require_owner)):
    data = _load_employees()
    data["employees"] = [e for e in data["employees"] if e["id"] != employee_id]
    _save_employees(data)
    assignments = _load_assignments()
    assignments["assignments"] = {k: v for k, v in assignments["assignments"].items() if v != employee_id}
    _save_assignments(assignments)
    return {"ok": True}


# ── Owner/Support: Assign ─────────────────────────────────────────────────────

@router.post("/api/admin/assign")
def assign_customer(req: AssignRequest, staff=Depends(require_support_or_above)):
    assignments = _load_assignments()
    if req.employee_id:
        assignments["assignments"][req.user_id] = req.employee_id
    else:
        assignments["assignments"].pop(req.user_id, None)
    _save_assignments(assignments)
    return {"ok": True}


# ── All staff: My customers ────────────────────────────────────────────────────

@router.get("/api/admin/my_customers")
def my_customers(staff=Depends(require_staff)):
    role = staff.get("role")
    assignments = _load_assignments()
    if role in ("owner", "support"):
        return _build_user_list(assignments)
    emp_id = staff.get("employee_id")
    my_ids = {uid for uid, eid in assignments.get("assignments", {}).items() if eid == emp_id}
    all_users = _build_user_list(assignments)
    return [u for u in all_users if u["id"] in my_ids]


# ── Employee: profile + change password ──────────────────────────────────────

@router.get("/api/admin/employee/me")
def employee_me(staff=Depends(require_staff)):
    if staff.get("role") == "owner":
        return {"role": "owner", "name": ADMIN_NAME, "email": ADMIN_EMAIL,
                "title": "Owner", "profile_image": ""}
    emp_id = staff.get("employee_id")
    data = _load_employees()
    emp = next((e for e in data["employees"] if e["id"] == emp_id), None)
    if not emp:
        raise HTTPException(status_code=404, detail="Medarbejder ikke fundet")
    return {
        "role": emp.get("role", "employee"),
        "name": emp["name"],
        "email": emp["email"],
        "title": emp.get("title", ""),
        "profile_image": emp.get("profile_image", ""),
    }


@router.put("/api/admin/employees/me")
def update_employee_me(req: UpdateEmployeeProfileRequest, staff=Depends(require_staff)):
    emp_id = staff.get("employee_id")
    if not emp_id:
        raise HTTPException(status_code=403, detail="Owner opdaterer ikke profil her")
    data = _load_employees()
    emp = next((e for e in data["employees"] if e["id"] == emp_id), None)
    if not emp:
        raise HTTPException(status_code=404, detail="Medarbejder ikke fundet")
    if req.name is not None:
        emp["name"] = req.name.strip()
    if req.title is not None:
        emp["title"] = req.title.strip()
    if req.profile_image is not None:
        emp["profile_image"] = req.profile_image.strip()
    _save_employees(data)
    return {"ok": True, "name": emp["name"], "title": emp["title"],
            "profile_image": emp.get("profile_image", "")}


@router.post("/api/admin/employee/change_password")
def employee_change_password(req: ChangePasswordRequest, staff=Depends(require_staff)):
    emp_id = staff.get("employee_id")
    if not emp_id:
        raise HTTPException(status_code=403, detail="Owner kan ikke skifte password her")
    data = _load_employees()
    emp = next((e for e in data["employees"] if e["id"] == emp_id), None)
    if not emp:
        raise HTTPException(status_code=404, detail="Medarbejder ikke fundet")
    if not pwd_context.verify(req.old_password, emp["hashed_password"]):
        raise HTTPException(status_code=401, detail="Forkert nuværende adgangskode")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Ny adgangskode skal være mindst 6 tegn")
    emp["hashed_password"] = pwd_context.hash(req.new_password)
    _save_employees(data)
    return {"ok": True}


# ── Guidelines ────────────────────────────────────────────────────────────────

@router.get("/api/admin/guidelines")
def get_guidelines(staff=Depends(require_staff)):
    return _load_guidelines()


@router.put("/api/admin/guidelines")
def update_guidelines(req: GuidelinesRequest, admin=Depends(require_owner)):
    data = _load_guidelines()
    if req.title is not None:
        data["title"] = req.title
    if req.sections is not None:
        data["sections"] = req.sections
    _save_guidelines(data)
    return {"ok": True}


# ── Support notes ─────────────────────────────────────────────────────────────

@router.post("/api/admin/support_note")
def add_support_note(req: SupportNoteRequest, staff=Depends(require_staff)):
    user_store = _load_user_store(req.user_id)
    note = {
        "id": str(uuid.uuid4())[:8],
        "text": req.text.strip(),
        "author": staff.get("display_email", staff.get("email", staff.get("name", "Admin"))),
        "created_at": datetime.utcnow().strftime("%d/%m %H:%M"),
        "resolved": False,
    }
    user_store.setdefault("support_notes", []).insert(0, note)
    _save_user_store(req.user_id, user_store)
    return {"ok": True, "note": note}


@router.post("/api/admin/resolve_note")
def resolve_note(req: ResolveNoteRequest, staff=Depends(require_staff)):
    user_store = _load_user_store(req.user_id)
    for note in user_store.get("support_notes", []):
        if note["id"] == req.note_id:
            note["resolved"] = True
            break
    _save_user_store(req.user_id, user_store)
    return {"ok": True}


@router.get("/api/admin/support_notes/{user_id}")
def get_support_notes(user_id: str, staff=Depends(require_staff)):
    user_store = _load_user_store(user_id)
    return user_store.get("support_notes", [])


# ── Owner: Ban / Pause / Delete users ────────────────────────────────────────

@router.post("/api/admin/users/{user_id}/ban")
def admin_ban_user(user_id: str, admin=Depends(require_owner)):
    user_store = _load_user_store(user_id)
    current = user_store.get("status", "active")
    new_status = "active" if current == "banned" else "banned"
    user_store["status"] = new_status
    _save_user_store(user_id, user_store)
    return {"ok": True, "status": new_status}


@router.post("/api/admin/users/{user_id}/pause")
def admin_pause_user(user_id: str, admin=Depends(require_owner)):
    user_store = _load_user_store(user_id)
    current = user_store.get("status", "active")
    new_status = "active" if current == "paused" else "paused"
    user_store["status"] = new_status
    _save_user_store(user_id, user_store)
    return {"ok": True, "status": new_status}


@router.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: str, admin=Depends(require_owner)):
    from services.users import load_users, save_users
    users_data = load_users()
    users_data["users"] = [u for u in users_data["users"] if u["id"] != user_id]
    save_users(users_data)
    data_path = os.path.join(USER_DATA_DIR, f"{user_id}.json")
    if os.path.exists(data_path):
        os.remove(data_path)
    assignments = _load_assignments()
    assignments["assignments"].pop(user_id, None)
    _save_assignments(assignments)
    return {"ok": True}


# ── Owner: API usage aggregation ─────────────────────────────────────────────

_ACTION_SERVICE = {
    "generate_pipeline": "Anthropic",
    "generate_voice":    "ElevenLabs",
    "generate_video":    "Runway",
    "generate_video_15": "Runway",
    "generate_video_30": "Runway",
    "assemble_video":    "Runway",
    "add_captions":      "Internal",
    "auto_post":         "Platforms",
}


@router.get("/api/admin/api_usage")
def admin_api_usage(admin=Depends(require_owner)):
    from services.users import load_users as _lu
    service_totals: dict = {}
    action_totals: dict = {}
    total_credits = 0
    for user in _lu().get("users", []):
        ustore = _load_user_store(user["id"])
        for entry in ustore.get("api_usage", []):
            action = entry.get("action", "unknown")
            credits = entry.get("credits", 0)
            service = _ACTION_SERVICE.get(action, "Other")
            service_totals[service] = service_totals.get(service, 0) + credits
            action_totals[action]   = action_totals.get(action, 0) + credits
            total_credits += credits
    return {
        "total_credits": total_credits,
        "by_service": service_totals,
        "by_action": action_totals,
    }


# ── Owner: Revenue overview ───────────────────────────────────────────────────

@router.get("/api/admin/revenue")
def admin_revenue(admin=Depends(require_owner)):
    from services.users import load_users as _lu
    plan_counts: dict = {k: 0 for k in PLANS}
    for user in _lu().get("users", []):
        ustore = _load_user_store(user["id"])
        plan = ustore.get("billing", {}).get("plan", "free")
        if plan not in PLANS:
            plan = "free"
        plan_counts[plan] = plan_counts.get(plan, 0) + 1
    plan_revenue = {k: plan_counts[k] * PLANS[k]["price"] for k in PLANS}
    mrr = sum(plan_revenue.values())
    return {
        "mrr": mrr,
        "arr": mrr * 12,
        "plan_counts": plan_counts,
        "plan_revenue": plan_revenue,
        "plans": {k: {"name": v["name"], "price": v["price"], "credits": v["credits"]}
                  for k, v in PLANS.items()},
    }


# ── Sub-account management (admin) ────────────────────────────────────────────

@router.get("/api/admin/users/{user_id}/subaccounts")
def admin_list_subaccounts(user_id: str, staff=Depends(require_support_or_above)):
    ustore = _load_user_store(user_id)
    billing = ustore.get("billing", {})
    return {
        "sub_accounts": ustore.get("sub_accounts", []),
        "extra_sub_slots": billing.get("extra_sub_slots", 0),
        "subs_blocked": billing.get("subs_blocked", False),
    }


@router.delete("/api/admin/users/{user_id}/subaccounts/{sub_id}")
def admin_delete_subaccount(user_id: str, sub_id: str, admin=Depends(require_owner)):
    ustore = _load_user_store(user_id)
    subs = ustore.get("sub_accounts", [])
    before = len(subs)
    ustore["sub_accounts"] = [s for s in subs if s["id"] != sub_id]
    if len(ustore.get("sub_accounts", {})) == before:
        raise HTTPException(status_code=404, detail="Sub-konto ikke fundet")
    _save_user_store(user_id, ustore)
    # Slet sub-konto data
    sub_store_path = os.path.join(USER_DATA_DIR, f"{user_id}_sub_{sub_id}.json")
    try:
        if os.path.exists(sub_store_path):
            os.remove(sub_store_path)
    except Exception:
        pass
    return {"ok": True}


@router.post("/api/admin/users/{user_id}/block_subs")
def admin_block_subs(user_id: str, body: dict, admin=Depends(require_owner)):
    """Bloker eller tillad oprettelse af under-konti for en bruger."""
    blocked = bool(body.get("blocked", True))
    ustore = _load_user_store(user_id)
    ustore.setdefault("billing", {})["subs_blocked"] = blocked
    _save_user_store(user_id, ustore)
    return {"ok": True, "subs_blocked": blocked}
