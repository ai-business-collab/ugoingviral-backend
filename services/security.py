"""
Security utilities for UgoingViral — rate limiting, headers, sanitization,
brute-force protection. Centralized so individual routes only need to import
the shared `limiter`, `record_login_failure`, etc.
"""
import os
import re
import json
import time
from threading import Lock
from typing import Optional

from fastapi import Request
from slowapi import Limiter
from slowapi.util import get_remote_address


# ── Rate limiter ─────────────────────────────────────────────────────────────
def _rate_key(request: Request) -> str:
    """Per-user when a JWT is present, otherwise per-IP."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            from jose import jwt as _jwt
            from routes.auth import SECRET_KEY, ALGORITHM
            payload = _jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
            uid = payload.get("user_id")
            if uid:
                return f"user:{uid}"
        except Exception:
            pass
    return f"ip:{get_remote_address(request)}"


limiter = Limiter(key_func=_rate_key, default_limits=["60/minute"])


# ── Security headers ─────────────────────────────────────────────────────────
SECURITY_HEADERS = {
    "X-Content-Type-Options":  "nosniff",
    "X-Frame-Options":         "DENY",
    "X-XSS-Protection":        "1; mode=block",
    "Referrer-Policy":         "strict-origin-when-cross-origin",
}


async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    for k, v in SECURITY_HEADERS.items():
        response.headers.setdefault(k, v)
    return response


# ── Body size limit ──────────────────────────────────────────────────────────
MAX_BODY_SIZE = 10 * 1024 * 1024  # 10 MB


async def body_size_middleware(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl:
        try:
            if int(cl) > MAX_BODY_SIZE:
                from fastapi.responses import JSONResponse
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body exceeds 10 MB limit"},
                )
        except ValueError:
            pass
    return await call_next(request)


# ── Input sanitization ───────────────────────────────────────────────────────
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SQLI_PATTERNS = [
    re.compile(r"(?i)\bunion\s+select\b"),
    re.compile(r"(?i)\bselect\b.+\bfrom\b.+\binformation_schema\b"),
    re.compile(r"(?i)\b(drop|truncate|delete)\s+table\b"),
    re.compile(r"(?i);\s*(drop|delete|update|insert)\s+"),
    re.compile(r"(?i)\bor\s+1\s*=\s*1\b"),
    re.compile(r"(?i)/\*.*?\*/"),
    re.compile(r"--\s*$", re.MULTILINE),
    re.compile(r"(?i)\bxp_cmdshell\b"),
]


def strip_html(value: str) -> str:
    """Remove HTML tags from a string. Safe to call on non-strings (returns as-is)."""
    if not isinstance(value, str):
        return value
    return _HTML_TAG_RE.sub("", value)


def sanitize_string(value: str) -> str:
    """Strip HTML tags and trim whitespace. Use on free-text user inputs."""
    if not isinstance(value, str):
        return value
    return _HTML_TAG_RE.sub("", value).strip()


def is_sqli_suspicious(value: str) -> bool:
    """True if the string matches any common SQL-injection pattern."""
    if not isinstance(value, str) or len(value) > 8192:
        return False
    return any(p.search(value) for p in _SQLI_PATTERNS)


# ── Brute-force tracking ─────────────────────────────────────────────────────
_BRUTE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "security_log.json")
_BRUTE_LOCK  = Lock()
_FAIL_WINDOW = 3600   # 1 hour
_FAIL_LIMIT  = 10


def _read_brute() -> dict:
    try:
        with open(_BRUTE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"failures": {}, "blocked": {}, "log": []}


def _write_brute(data: dict) -> None:
    try:
        with open(_BRUTE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def is_ip_blocked(ip: str) -> bool:
    """True if the IP is in the blocked list and the block is still active."""
    if not ip:
        return False
    with _BRUTE_LOCK:
        data = _read_brute()
        until = data.get("blocked", {}).get(ip)
        if not until:
            return False
        if time.time() >= until:
            data["blocked"].pop(ip, None)
            _write_brute(data)
            return False
        return True


def record_login_failure(ip: str, email: str = "") -> Optional[float]:
    """Record a failed login. Returns the block-expiry timestamp if newly blocked."""
    if not ip:
        return None
    now = time.time()
    with _BRUTE_LOCK:
        data = _read_brute()
        fails = [t for t in data.get("failures", {}).get(ip, []) if now - t < _FAIL_WINDOW]
        fails.append(now)
        data.setdefault("failures", {})[ip] = fails
        data.setdefault("log", []).append({
            "ts": now, "event": "login_failure", "ip": ip, "email": email[:120],
        })
        # Trim log to last 1000 entries
        if len(data["log"]) > 1000:
            data["log"] = data["log"][-1000:]
        blocked_until = None
        if len(fails) >= _FAIL_LIMIT:
            blocked_until = now + _FAIL_WINDOW
            data.setdefault("blocked", {})[ip] = blocked_until
            data["log"].append({
                "ts": now, "event": "ip_blocked", "ip": ip,
                "reason": f"{_FAIL_LIMIT}+ failed logins in {_FAIL_WINDOW}s",
            })
        _write_brute(data)
        return blocked_until


def record_login_success(ip: str, email: str = "") -> None:
    """Clear failure history for this IP on successful login."""
    if not ip:
        return
    with _BRUTE_LOCK:
        data = _read_brute()
        data.get("failures", {}).pop(ip, None)
        data.setdefault("log", []).append({
            "ts": time.time(), "event": "login_success", "ip": ip, "email": email[:120],
        })
        if len(data["log"]) > 1000:
            data["log"] = data["log"][-1000:]
        _write_brute(data)


def client_ip(request: Request) -> str:
    """Best-effort client IP — honors X-Forwarded-For when behind a proxy."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request) or ""
