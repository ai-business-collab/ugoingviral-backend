import os
import httpx
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from services.users import create_user, get_user_by_email, get_user_by_id, update_user, verify_password

router = APIRouter()

SECRET_KEY = os.getenv("JWT_SECRET", "ugv-jwt-secret-change-in-production-2026")
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30

security = HTTPBearer(auto_error=False)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://ugoingviral.com/api/auth/google/callback")


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    company: str = ""
    ref: str = ""


class UpdateProfileRequest(BaseModel):
    name: str = ""
    company: str = ""
    cvr: str = ""
    website: str = ""
    address: str = ""
    industry: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


def _create_token(user_id: str, email: str) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": email, "user_id": user_id, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _safe_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user.get("name", ""), "company": user.get("company", "")}


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Token mangler")
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        user_id: str = payload.get("user_id")
        user = get_user_by_id(user_id) if user_id else None
        if not user or not user.get("is_active"):
            raise HTTPException(status_code=401, detail="Bruger ikke fundet")
        return user
    except JWTError:
        raise HTTPException(status_code=401, detail="Ugyldig eller udløbet token")


@router.post("/api/auth/register")
async def register(req: RegisterRequest):
    if not req.email or "@" not in req.email:
        raise HTTPException(status_code=400, detail="Ugyldig email-adresse")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Adgangskode skal være mindst 6 tegn")
    if get_user_by_email(req.email):
        raise HTTPException(status_code=400, detail="Email er allerede i brug")
    user = create_user(req.email, req.password, req.name, req.company)
    token = _create_token(user["id"], user["email"])
    try:
        import asyncio
        from routes.email import send_welcome_email
        asyncio.get_event_loop().run_in_executor(None, send_welcome_email, user["email"], user.get("name", ""))
    except Exception:
        pass
    if req.ref:
        try:
            from routes.billing import referral_signup
            referral_signup({"ref_code": req.ref, "user_id": user["id"]})
        except Exception:
            pass
    return {"token": token, "user": _safe_user(user)}


@router.post("/api/auth/login")
async def login(req: LoginRequest):
    user = get_user_by_email(req.email)
    if not user or not verify_password(req.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="Forkert email eller adgangskode")
    token = _create_token(user["id"], user["email"])
    # Auto daily login bonus — requires user store context
    from services.store import _load_user_store, _save_user_store
    import datetime as _dt
    try:
        ustore = _load_user_store(user["id"])
        billing = ustore.setdefault("billing", {})
        today = _dt.date.today().isoformat()
        if billing.get("last_daily_bonus") != today:
            billing["last_daily_bonus"] = today
            claimed = billing.setdefault("claimed_bonuses", [])
            plan_key = billing.get("plan", "free")
            from routes.billing import PLANS, BONUS_CREDITS
            plan_info = PLANS.get(plan_key, PLANS["free"])
            billing["credits"] = billing.get("credits", plan_info["credits"]) + BONUS_CREDITS["daily_login"]
            ustore["billing"] = billing
            _save_user_store(user["id"], ustore)
    except Exception:
        pass
    # Track last_login timestamp
    try:
        update_user(user["id"], {"last_login": __import__('datetime').datetime.utcnow().isoformat()})
    except Exception:
        pass
    return {"token": token, "user": _safe_user(user)}


@router.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    return _safe_user(current_user)


@router.patch("/api/auth/profile")
async def update_profile(req: UpdateProfileRequest, current_user: dict = Depends(get_current_user)):
    updates = {"name": req.name.strip(), "company": req.company.strip()}
    for f in ("cvr", "website", "address", "industry"):
        v = getattr(req, f, "").strip()
        if v is not None:
            updates[f] = v
    updated = update_user(current_user["id"], updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Bruger ikke fundet")
    return _safe_user(updated)


@router.get("/api/auth/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google login ikke konfigureret")
    import urllib.parse
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "access_type": "offline",
        "prompt": "select_account",
    }
    url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@router.get("/api/auth/google/callback")
async def google_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?error=google_cancelled")
    if not GOOGLE_CLIENT_ID:
        return RedirectResponse("/?error=google_not_configured")

    async with httpx.AsyncClient() as client:
        # Exchange code for tokens
        token_resp = await client.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            return RedirectResponse("/?error=google_token_failed")
        token_data = token_resp.json()

        # Get user info
        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        if userinfo_resp.status_code != 200:
            return RedirectResponse("/?error=google_userinfo_failed")
        info = userinfo_resp.json()

    email = info.get("email", "")
    name = info.get("name", "")
    if not email:
        return RedirectResponse("/?error=google_no_email")

    user = get_user_by_email(email)
    if not user:
        user = create_user(email, os.urandom(24).hex(), name, "")
        try:
            from routes.email import send_welcome_email
            import asyncio
            asyncio.get_event_loop().run_in_executor(None, send_welcome_email, email, name)
        except Exception:
            pass

    if not user.get("is_active"):
        return RedirectResponse("/?error=account_inactive")

    token = _create_token(user["id"], user["email"])
    return RedirectResponse(f"/app?token={token}")
