"""
Security utilities for UgoingViral — rate limiting, headers, sanitization,
brute-force protection. Centralized so individual routes only need to import
the shared `limiter`, `record_login_failure`, etc.
"""
import os
import re
import json
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
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


# Global default: 60 requests / minute per user (or per IP if unauthenticated).
# In-memory storage (no Redis) — fine for a single-process deployment.
limiter = Limiter(key_func=_rate_key, default_limits=["60/minute"])

_FRIENDLY_RATE_MSG = "Too many requests — please wait a moment"


def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    """Friendly 429 for any limit breach (global 60/min or per-route limits)."""
    return JSONResponse(
        status_code=429,
        content={"detail": _FRIENDLY_RATE_MSG},
        headers={"Retry-After": "60"},
    )


# ── Video-generation limiter (10 / hour / user, in-memory sliding window) ─────
_VIDEO_LIMIT  = 10
_VIDEO_WINDOW = 3600  # seconds
_video_lock   = Lock()
_video_hits: dict = defaultdict(deque)


def check_video_rate_limit(user_id: str) -> bool:
    """Return True if the user may start another video render, recording the hit.
    Returns False once they have hit 10 renders within the last hour."""
    if not user_id:
        return True
    now = time.time()
    with _video_lock:
        dq = _video_hits[user_id]
        while dq and now - dq[0] >= _VIDEO_WINDOW:
            dq.popleft()
        if len(dq) >= _VIDEO_LIMIT:
            return False
        dq.append(now)
        return True


def video_rate_limit_message() -> str:
    return (f"Too many requests — you can generate up to {_VIDEO_LIMIT} videos "
            f"per hour. Please wait a moment and try again.")


# ── Security headers ─────────────────────────────────────────────────────────
SECURITY_HEADERS = {
    "X-Content-Type-Options":  "nosniff",
    "X-Frame-Options":         "DENY",
    "X-XSS-Protection":        "1; mode=block",
    "Referrer-Policy":         "strict-origin-when-cross-origin",
    # Force HTTPS for a year (incl. subdomains) — site is HTTPS-only behind nginx.
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    # Conservative CSP: blocks clickjacking, plugin/object injection and <base>
    # hijacking WITHOUT restricting the SPA's inline scripts/styles (which would
    # break the existing frontend). Tighten script-src in a future pass once the
    # frontend is refactored away from inline handlers.
    "Content-Security-Policy": "frame-ancestors 'none'; object-src 'none'; base-uri 'self'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
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


# Genuine XSS injection vectors — narrow on purpose so legitimate marketing copy
# (which may mention words like "onload" in prose) is not falsely rejected.
_XSS_PATTERNS = [
    re.compile(r"(?i)<\s*script\b"),
    re.compile(r"(?i)<\s*/\s*script\s*>"),
    re.compile(r"(?i)<\s*iframe\b"),
    re.compile(r"(?i)javascript\s*:"),
    re.compile(r"(?i)<\s*img[^>]+\bonerror\s*="),
    re.compile(r"(?i)\bon(error|load|click|mouseover)\s*=\s*['\"]"),
]


def is_xss_suspicious(value: str) -> bool:
    """True if the string contains a recognized XSS injection vector."""
    if not isinstance(value, str) or len(value) > 100_000:
        return False
    return any(p.search(value) for p in _XSS_PATTERNS)


def scan_payload(value, _depth: int = 0) -> Optional[str]:
    """Recursively scan a decoded JSON payload for SQLi / XSS in any string.
    Returns a human-readable reason on the first hit, else None."""
    if _depth > 12:
        return None
    if isinstance(value, str):
        if is_sqli_suspicious(value):
            return "Input rejected: looks like a SQL-injection attempt."
        if is_xss_suspicious(value):
            return "Input rejected: looks like an XSS / script-injection attempt."
        return None
    if isinstance(value, dict):
        for v in value.values():
            reason = scan_payload(v, _depth + 1)
            if reason:
                return reason
        return None
    if isinstance(value, (list, tuple)):
        for v in value:
            reason = scan_payload(v, _depth + 1)
            if reason:
                return reason
    return None


# ── File-type validation by content (magic bytes, no external deps) ───────────
def sniff_media_type(data: bytes) -> Optional[str]:
    """Return a coarse media kind ('video'/'audio'/'image') based on the file's
    real leading bytes, or None if it doesn't match a known signature."""
    if not data or len(data) < 12:
        return None
    head = data[:16]

    # MP4 / MOV / M4A — ISO base media: bytes 4..8 == 'ftyp'
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand[:3] in (b"M4A", b"m4a"):
            return "audio"
        return "video"
    # RIFF containers — AVI (video) and WAV (audio)
    if head[:4] == b"RIFF":
        if head[8:12] == b"AVI ":
            return "video"
        if head[8:12] == b"WAVE":
            return "audio"
    # MP3 — ID3 tag or MPEG audio frame sync
    if head[:3] == b"ID3":
        return "audio"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "audio"
    # Common images
    if head[:3] == b"\xff\xd8\xff":
        return "image"            # JPEG
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image"            # PNG
    if head[:4] == b"GIF8":
        return "image"            # GIF
    if head[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image"            # WEBP
    return None


def media_kind_matches(data: bytes, expected_kind: str) -> bool:
    """True if the real bytes match the expected kind ('video' or 'audio')."""
    sniffed = sniff_media_type(data)
    return sniffed == expected_kind


# ── Brute-force tracking ─────────────────────────────────────────────────────
_BRUTE_FILE     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "security_log.json")
_BRUTE_LOCK     = Lock()
_FAIL_WINDOW    = 300    # detection window — 5 minutes
_FAIL_LIMIT     = 10     # block after 10 failed logins inside the window
_BLOCK_DURATION = 900    # stay blocked for 15 minutes


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
            blocked_until = now + _BLOCK_DURATION
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


# ── Token revocation (logout) ────────────────────────────────────────────────
# JWTs are stateless, so "logout" must be enforced server-side via a denylist.
# We store a SHA-256 of each revoked token plus its original expiry, so the file
# stays small (expired entries are purged on access). Shared across workers via
# the file on disk (the auth path is low-volume, so file IO is acceptable here).
import hashlib as _hashlib

_REVOKED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "revoked_tokens.json")
_REVOKED_LOCK = Lock()
_revoked_cache: dict = {}
_revoked_mtime: float = 0.0


def _token_hash(token: str) -> str:
    return _hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_revoked() -> dict:
    try:
        with open(_REVOKED_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_revoked(data: dict) -> None:
    try:
        with open(_REVOKED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def revoke_token(token: str, exp_ts: Optional[float] = None) -> None:
    """Add a token to the denylist (called on logout). `exp_ts` is the token's
    own expiry epoch seconds so the entry can be purged once it would expire
    on its own anyway."""
    if not token:
        return
    now = time.time()
    if exp_ts is None:
        exp_ts = now + 30 * 24 * 3600  # default to max token lifetime
    with _REVOKED_LOCK:
        data = _read_revoked()
        # Purge entries that have already expired.
        data = {h: e for h, e in data.items() if e > now}
        data[_token_hash(token)] = exp_ts
        _write_revoked(data)
        global _revoked_cache, _revoked_mtime
        _revoked_cache = data
        try:
            _revoked_mtime = os.path.getmtime(_REVOKED_FILE)
        except Exception:
            _revoked_mtime = now


def is_token_revoked(token: str) -> bool:
    """True if this exact token has been revoked (logged out) and not yet
    expired. Uses an mtime-cached read so the hot auth path avoids re-parsing
    the file on every request."""
    if not token:
        return False
    global _revoked_cache, _revoked_mtime
    try:
        mtime = os.path.getmtime(_REVOKED_FILE)
    except OSError:
        return False  # no file → nothing revoked
    if mtime != _revoked_mtime:
        with _REVOKED_LOCK:
            _revoked_cache = _read_revoked()
            _revoked_mtime = mtime
    exp = _revoked_cache.get(_token_hash(token))
    return bool(exp and exp > time.time())


def client_ip(request: Request) -> str:
    """Best-effort client IP — honors X-Forwarded-For when behind a proxy."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request) or ""


# ── Global input-validation middleware (pure ASGI so it can safely buffer and
#    replay the request body) ─────────────────────────────────────────────────
class InputValidationMiddleware:
    """Scan every request for SQL-injection / XSS payloads and reject them with
    a clear 400 before they reach any route handler.

    - Query strings are scanned on all methods.
    - JSON bodies are buffered, scanned, then replayed downstream untouched.
    - multipart/form-data (file uploads) and other body types are passed
      through unbuffered so large uploads are never held in memory.
    """

    # Trusted, signature-verified server-to-server endpoints — never scan these
    # so a benign payload can't be rejected (e.g. a Stripe payment webhook).
    SKIP_PREFIXES = ("/api/billing/webhook", "/api/webhooks/")

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        path = scope.get("path", "")
        if any(path.startswith(p) for p in self.SKIP_PREFIXES):
            return await self.app(scope, receive, send)

        # 1) Query string — cheap, scan for every method.
        qs = scope.get("query_string", b"")
        if qs:
            try:
                from urllib.parse import unquote_plus
                decoded = unquote_plus(qs.decode("utf-8", "ignore"))
            except Exception:
                decoded = ""
            if is_sqli_suspicious(decoded) or is_xss_suspicious(decoded):
                return await self._reject(scope, receive, send,
                                          "Input rejected: suspicious query parameters.")

        method = scope.get("method", "")
        ctype = ""
        for k, v in scope.get("headers", []):
            if k == b"content-type":
                ctype = v.decode("latin-1", "ignore").lower()
                break

        # 2) JSON body — buffer + scan + replay. Skip non-JSON (e.g. uploads).
        if method in ("POST", "PUT", "PATCH") and "application/json" in ctype:
            body = b""
            messages = []
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] == "http.request":
                    body += message.get("body", b"")
                    more_body = message.get("more_body", False)
                else:  # http.disconnect
                    messages.append(message)
                    more_body = False
            if body:
                try:
                    payload = json.loads(body)
                    reason = scan_payload(payload)
                except Exception:
                    reason = None  # not valid JSON — let the route handle it
                if reason:
                    return await self._reject(scope, receive, send, reason)

            replayed = False

            async def replay_receive():
                nonlocal replayed
                if not replayed:
                    replayed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                if messages:
                    return messages.pop(0)
                return await receive()

            return await self.app(scope, replay_receive, send)

        return await self.app(scope, receive, send)

    async def _reject(self, scope, receive, send, detail: str):
        response = JSONResponse(status_code=400, content={"detail": detail})
        await response(scope, receive, send)
