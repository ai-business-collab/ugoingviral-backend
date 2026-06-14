import os
import secrets
import httpx
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel

from services.users import (
    create_user, get_user_by_email, get_user_by_id, update_user, verify_password,
    acreate_user, aget_user_by_email, aupdate_user, verify_password_async,
)
from services.security import (
    limiter,
    sanitize_string,
    is_sqli_suspicious,
    is_ip_blocked,
    record_login_failure,
    record_login_success,
    client_ip,
    revoke_token,
    is_token_revoked,
)

router = APIRouter()

# Fail CLOSED on the JWT signing key. Previously this fell back to a hardcoded
# default ("ugv-jwt-secret-change-in-production-2026"); if .env ever failed to
# load, the app would have run with a publicly-known key, letting anyone forge
# tokens for any user. Refuse to start instead.
_DEFAULT_JWT_SECRET = "ugv-jwt-secret-change-in-production-2026"
SECRET_KEY = os.getenv("JWT_SECRET", "")
if not SECRET_KEY or SECRET_KEY == _DEFAULT_JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is missing (or set to the insecure default). Refusing to "
        "start — set a strong JWT_SECRET in .env before launch."
    )
ALGORITHM = "HS256"
TOKEN_EXPIRE_DAYS = 30

security = HTTPBearer(auto_error=False)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "https://ugoingviral.com/api/auth/google/callback")

GITHUB_CLIENT_ID     = os.getenv("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.getenv("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI  = os.getenv("GITHUB_REDIRECT_URI", "https://ugoingviral.com/api/auth/github/callback")


class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str = ""
    company: str = ""
    niche: str = ""
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


class VerifyEmailRequest(BaseModel):
    email: str
    code: str


class ResendVerificationRequest(BaseModel):
    email: str


VERIFICATION_TTL_MIN = 30


def _generate_verification_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


async def _issue_verification(user: dict) -> str:
    code = _generate_verification_code()
    expires = (datetime.utcnow() + timedelta(minutes=VERIFICATION_TTL_MIN)).isoformat()
    # Async DB write — never block the event loop (called on the hot register/
    # login path).
    await aupdate_user(user["id"], {
        "email_verified": False,
        "verification_code": code,
        "verification_expires": expires,
    })
    return code


def _send_verification_email_async(email: str, name: str, code: str) -> None:
    try:
        import asyncio
        from routes.email import send_verification_email
        if email.startswith("qa-"):
            return
        asyncio.get_event_loop().run_in_executor(None, send_verification_email, email, name, code)
    except Exception:
        pass


def _create_token(user_id: str, email: str) -> str:
    expire = datetime.utcnow() + timedelta(days=TOKEN_EXPIRE_DAYS)
    return jwt.encode(
        {"sub": email, "user_id": user_id, "exp": expire},
        SECRET_KEY,
        algorithm=ALGORITHM,
    )


def _safe_user(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "name": user.get("name", ""), "company": user.get("company", ""), "niche": user.get("niche", ""), "created_at": user.get("created_at", "")}


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=401, detail="Token mangler")
    # Reject tokens that have been logged out (revoked) — enforces real logout
    # on otherwise-stateless JWTs.
    if is_token_revoked(credentials.credentials):
        raise HTTPException(status_code=401, detail="Token er udløbet — log ind igen")
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
@limiter.limit("5/minute")
async def register(request: Request, req: RegisterRequest):
    if not req.email or "@" not in req.email:
        raise HTTPException(status_code=400, detail="Ugyldig email-adresse")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Adgangskode skal være mindst 6 tegn")
    # Sanitize free-text fields and reject obvious injection attempts on email
    if is_sqli_suspicious(req.email) or is_sqli_suspicious(req.name) or is_sqli_suspicious(req.company):
        raise HTTPException(status_code=400, detail="Ugyldigt input")
    req.email   = sanitize_string(req.email).lower()
    req.name    = sanitize_string(req.name)
    req.company = sanitize_string(req.company)
    req.niche   = sanitize_string(req.niche)
    if await aget_user_by_email(req.email):
        raise HTTPException(status_code=400, detail="Email er allerede i brug")
    user = await acreate_user(req.email, req.password, req.name, req.company, req.niche)
    try:
        from services.db_sync import pg_create_user
        pg_create_user(user)
    except Exception:
        pass
    try:
        from routes.workspaces import _ensure_default_workspace
        from services.store import _load_user_store, _save_user_store
        _ustore = _load_user_store(user["id"])
        _ustore = _ensure_default_workspace(_ustore)
        # New-user defaults: free plan + 50 starter credits, and a fresh
        # free-video allowance (3 free AI videos for every new user).
        _billing = _ustore.get("billing") or {}
        _billing["plan"] = _billing.get("plan") or "free"
        _billing["credits"] = 50
        _ustore["billing"] = _billing
        _ustore["free_videos_used"] = 0
        _ustore["free_videos_skipped"] = False
        _save_user_store(user["id"], _ustore)
    except Exception:
        pass
    # Issue verification code, send welcome + verification emails.
    code = await _issue_verification(user)
    try:
        import asyncio
        from routes.email import send_welcome_email
        if not user["email"].startswith("qa-"): asyncio.get_event_loop().run_in_executor(None, send_welcome_email, user["email"], user.get("name", ""))
    except Exception:
        pass
    _send_verification_email_async(user["email"], user.get("name", ""), code)
    if req.ref:
        try:
            from routes.billing import referral_signup
            referral_signup({"ref_code": req.ref, "user_id": user["id"]})
        except Exception:
            pass
    return {"requires_verification": True, "email": user["email"]}


@router.post("/api/auth/login")
@limiter.limit("10/minute")
async def login(request: Request, req: LoginRequest):
    ip = client_ip(request)
    if is_ip_blocked(ip):
        raise HTTPException(status_code=429, detail="Too many failed attempts — try again later")
    user = await aget_user_by_email(req.email)
    if not user or not await verify_password_async(req.password, user["hashed_password"]):
        record_login_failure(ip, req.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    record_login_success(ip, req.email)
    # Block unverified accounts. Older accounts without the field are treated as verified.
    if user.get("email_verified") is False:
        code = await _issue_verification(user)
        _send_verification_email_async(user["email"], user.get("name", ""), code)
        return {"requires_verification": True, "email": user["email"]}
    token = _create_token(user["id"], user["email"])
    # Auto daily login bonus — requires user store context
    from services.store import _load_user_store, _save_user_store
    import datetime as _dt
    try:
        ustore = _load_user_store(user["id"])
        billing = ustore.setdefault("billing", {})
        claimed = billing.setdefault("claimed_bonuses", [])
        if "daily_login" not in claimed:
            claimed.append("daily_login")
            plan_key = billing.get("plan", "free")
            from routes.billing import PLANS, BONUS_CREDITS
            plan_info = PLANS.get(plan_key, PLANS["free"])
            billing["credits"] = billing.get("credits", plan_info["credits"]) + BONUS_CREDITS["daily_login"]
            billing["claimed_bonuses"] = claimed
            ustore["billing"] = billing
            _save_user_store(user["id"], ustore)
    except Exception:
        pass
    # Track last_login timestamp
    try:
        await aupdate_user(user["id"], {"last_login": __import__('datetime').datetime.utcnow().isoformat()})
    except Exception:
        pass
    return {"token": token, "user": _safe_user(user)}


@router.post("/api/auth/verify_email")
@limiter.limit("10/minute")
async def verify_email(request: Request, req: VerifyEmailRequest):
    email = sanitize_string(req.email).lower()
    code = (req.code or "").strip()
    user = await aget_user_by_email(email)
    if not user:
        raise HTTPException(status_code=404, detail="No account found for this email")
    if user.get("email_verified", True):
        token = _create_token(user["id"], user["email"])
        return {"token": token, "user": _safe_user(user)}
    stored = (user.get("verification_code") or "").strip()
    expires_raw = user.get("verification_expires") or ""
    if not stored or stored != code:
        raise HTTPException(status_code=400, detail="Invalid verification code")
    try:
        if expires_raw and datetime.fromisoformat(expires_raw) < datetime.utcnow():
            raise HTTPException(status_code=400, detail="Verification code has expired — request a new one")
    except ValueError:
        pass
    await aupdate_user(user["id"], {
        "email_verified": True,
        "verification_code": "",
        "verification_expires": "",
    })
    user = await aget_user_by_email(email)
    token = _create_token(user["id"], user["email"])
    return {"token": token, "user": _safe_user(user)}


@router.post("/api/auth/resend_verification")
@limiter.limit("3/minute")
async def resend_verification(request: Request, req: ResendVerificationRequest):
    email = sanitize_string(req.email).lower()
    user = await aget_user_by_email(email)
    # Don't leak whether the email exists.
    if not user or user.get("email_verified", True):
        return {"ok": True}
    code = await _issue_verification(user)
    _send_verification_email_async(user["email"], user.get("name", ""), code)
    return {"ok": True}


@router.get("/api/auth/me")
async def me(current_user: dict = Depends(get_current_user)):
    return _safe_user(current_user)


@router.post("/api/auth/logout")
async def logout(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Revoke the presented bearer token so it can no longer be used, even
    though JWTs are otherwise valid until their 30-day expiry. The client also
    drops the token locally; this guarantees a stolen/copied token is dead after
    logout."""
    if not credentials:
        # Nothing to revoke — treat as a no-op success so logout is idempotent.
        return {"ok": True}
    token = credentials.credentials
    exp_ts = None
    try:
        # Decode WITHOUT verifying exp so we can still revoke a near-expiry token,
        # and read its real exp to size the denylist entry.
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM],
                             options={"verify_exp": False})
        exp_ts = payload.get("exp")
    except JWTError:
        pass
    revoke_token(token, exp_ts)
    return {"ok": True}


@router.patch("/api/auth/profile")
async def update_profile(req: UpdateProfileRequest, current_user: dict = Depends(get_current_user)):
    updates = {"name": req.name.strip(), "company": req.company.strip()}
    for f in ("cvr", "website", "address", "industry"):
        v = getattr(req, f, "").strip()
        if v is not None:
            updates[f] = v
    updated = await aupdate_user(current_user["id"], updates)
    if not updated:
        raise HTTPException(status_code=404, detail="Bruger ikke fundet")
    return _safe_user(updated)


@router.get("/api/auth/google/login")
def google_login():
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google login not configured")
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

    user = await aget_user_by_email(email)
    if not user:
        user = await acreate_user(email, os.urandom(24).hex(), name, "")
        try:
            from routes.email import send_welcome_email
            import asyncio
            if not email.startswith("qa-"): asyncio.get_event_loop().run_in_executor(None, send_welcome_email, email, name)
        except Exception:
            pass
    # OAuth provider already verified the email — mark verified.
    await aupdate_user(user["id"], {"email_verified": True, "verification_code": "", "verification_expires": ""})

    if not user.get("is_active"):
        return RedirectResponse("/?error=account_inactive")

    token = _create_token(user["id"], user["email"])
    return RedirectResponse(f"/app?token={token}")



@router.get("/api/auth/github/login")
def github_login():
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub login not configured")
    import urllib.parse
    params = {
        "client_id":    GITHUB_CLIENT_ID,
        "redirect_uri": GITHUB_REDIRECT_URI,
        "scope":        "user:email",
    }
    url = "https://github.com/login/oauth/authorize?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@router.get("/api/auth/github/callback")
async def github_callback(code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/?error=github_cancelled")
    if not GITHUB_CLIENT_ID:
        return RedirectResponse("/?error=github_not_configured")

    async with httpx.AsyncClient(timeout=15) as client:
        # Exchange code for access token
        token_resp = await client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id":     GITHUB_CLIENT_ID,
                "client_secret": GITHUB_CLIENT_SECRET,
                "code":          code,
                "redirect_uri":  GITHUB_REDIRECT_URI,
            },
        )
        if token_resp.status_code != 200:
            return RedirectResponse("/?error=github_token_failed")
        token_data = token_resp.json()
        access_token = token_data.get("access_token", "")
        if not access_token:
            return RedirectResponse("/?error=github_token_failed")

        gh_headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept":        "application/vnd.github.v3+json",
        }

        # Get user profile
        user_resp = await client.get("https://api.github.com/user", headers=gh_headers)
        if user_resp.status_code != 200:
            return RedirectResponse("/?error=github_userinfo_failed")
        gh_user = user_resp.json()

        # Primary email may be null if user keeps it private — fetch /user/emails
        email = gh_user.get("email") or ""
        if not email:
            emails_resp = await client.get(
                "https://api.github.com/user/emails", headers=gh_headers
            )
            if emails_resp.status_code == 200:
                for entry in emails_resp.json():
                    if entry.get("primary") and entry.get("verified"):
                        email = entry.get("email", "")
                        break
                if not email:
                    # Fallback: any verified email
                    for entry in emails_resp.json():
                        if entry.get("verified"):
                            email = entry.get("email", "")
                            break

    if not email:
        return RedirectResponse("/?error=github_no_email")

    name = gh_user.get("name") or gh_user.get("login") or ""

    user = await aget_user_by_email(email)
    if not user:
        user = await acreate_user(email, os.urandom(24).hex(), name, "")
        try:
            from routes.email import send_welcome_email
            import asyncio
            if not email.startswith("qa-"): asyncio.get_event_loop().run_in_executor(None, send_welcome_email, email, name)
        except Exception:
            pass
    # OAuth provider already verified the email — mark verified.
    await aupdate_user(user["id"], {"email_verified": True, "verification_code": "", "verification_expires": ""})

    if not user.get("is_active"):
        return RedirectResponse("/?error=account_inactive")

    token = _create_token(user["id"], user["email"])
    return RedirectResponse(f"/app?token={token}")


@router.patch("/api/auth/niche")
async def set_niche(req: Request, current_user: dict = Depends(get_current_user)):
    d = await req.json()
    niche = (d.get("niche") or "").strip()
    if not niche:
        raise HTTPException(status_code=400, detail="niche required")
    await aupdate_user(current_user["id"], {"niche": niche})
    return {"ok": True, "niche": niche}
