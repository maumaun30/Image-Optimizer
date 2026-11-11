"""Google sign-in restricted to allowlisted Workspace domains.

The API runs the OAuth code flow itself and keeps the session in a signed httpOnly
cookie on the shared parent domain (SESSION_COOKIE_DOMAIN). Download links, XHR
video uploads and fetch calls all carry it automatically, and no token is exposed to
page JavaScript.

Access is decided by the ID token's `hd` (hosted domain) claim, which Google only sets
for Workspace accounts. A consumer Gmail account has no `hd` and is always refused,
whatever its address looks like.

Stdlib only: HMAC-SHA256 signs the cookies, and the ID token is read straight from
Google's token endpoint over TLS, which OIDC Core §3.1.3.7 accepts in place of
verifying its signature.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

SESSION_COOKIE = "imageopt_session"
STATE_COOKIE = "imageopt_oauth_state"
STATE_TTL_SECONDS = 600

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class User:
    email: str
    name: str
    is_admin: bool


def auth_disabled() -> bool:
    """Local-dev escape hatch. Honoured only with DEBUG on, so a stray AUTH_DISABLED in
    a production .env can't open the API."""
    return settings.AUTH_DISABLED and settings.DEBUG


def check_auth_config() -> None:
    """Called at startup: refuse to boot with auth half-configured."""
    if auth_disabled():
        logger.warning("AUTH_DISABLED: every request runs as %s", settings.DEV_USER_EMAIL)
        return
    missing = [
        name
        for name in (
            "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "AUTH_REDIRECT_URL",
            "FRONTEND_URL", "SESSION_SECRET", "ALLOWED_DOMAINS",
        )
        if not getattr(settings, name)
    ]
    if missing:
        raise RuntimeError(f"Auth is not configured; set {', '.join(missing)} in .env")
    if len(settings.SESSION_SECRET) < 32:
        raise RuntimeError("SESSION_SECRET must be at least 32 characters")
    if "*" in settings.CORS_ORIGINS:
        # With credentials allowed, a wildcard would let any site make signed-in calls
        raise RuntimeError("CORS_ORIGINS cannot be '*' when auth is on; list the frontend URL")


# ---- signed cookies ----


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _sign(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(settings.SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    return f"{body}.{_b64(sig)}"


def _unsign(value: str | None) -> dict | None:
    """Return the payload if the signature is valid and it hasn't expired."""
    if not value or "." not in value:
        return None
    body, sig = value.rsplit(".", 1)
    expected = hmac.new(settings.SESSION_SECRET.encode(), body.encode(), hashlib.sha256).digest()
    try:
        if not hmac.compare_digest(_unb64(sig), expected):
            return None
        payload = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        return None
    return payload


def _set_cookie(response: Response, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="lax",
        domain=settings.SESSION_COOKIE_DOMAIN or None,
        path="/",
    )


def _clear_cookie(response: Response, name: str) -> None:
    response.delete_cookie(name, domain=settings.SESSION_COOKIE_DOMAIN or None, path="/")


# ---- request dependency ----


def _check_origin(request: Request) -> None:
    """CSRF backstop. SameSite=Lax already stops cross-site posts, but sibling
    subdomains count as same-site, so state-changing requests must also come from a
    listed frontend origin."""
    if request.method not in UNSAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in {o.rstrip("/") for o in settings.CORS_ORIGINS}:
        raise HTTPException(403, "Request origin not allowed")


def current_user(request: Request) -> User:
    if auth_disabled():
        return User(settings.DEV_USER_EMAIL, "Developer", True)

    _check_origin(request)

    for value in _session_cookie_values(request):
        session = _unsign(value)
        # Re-check the domain on every request so removing it from ALLOWED_DOMAINS
        # takes effect immediately, not when the session expires.
        if session and session.get("hd") in settings.ALLOWED_DOMAINS:
            email = session["email"]
            return User(email, session.get("name", email), email.lower() in settings.ADMIN_EMAILS)
    raise HTTPException(401, "Sign in required")


def _session_cookie_values(request: Request) -> list[str]:
    """Every session cookie the browser sent, not just one.

    A browser can hold two cookies with the same name for different domains — e.g.
    after moving SESSION_COOKIE_DOMAIN, an old `.example.com` one alongside the new
    `.app.example.com` one, and it sends both. `request.cookies` keeps only one of
    them, which may be the stale one; trying each means any valid session works.
    """
    values = []
    for part in request.headers.get("cookie", "").split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name == SESSION_COOKIE and value:
            values.append(value.strip('"'))
    return values


def _needs_auth_before_body(scope) -> bool:
    """Requests that carry an upload body: classic multipart uploads and the
    resumable (tus) create/append calls."""
    method, path = scope["method"], scope["path"]
    if method == "POST" and path.endswith("/upload"):
        return True
    return method in ("POST", "PATCH") and path.startswith("/video/tus")


class UploadAuthMiddleware:
    """Reject unauthenticated uploads before their body is read.

    FastAPI parses a multipart body (spooling it to temp files) before it runs any
    dependency, so `current_user` alone would still let an anonymous client push
    gigabytes onto the disk before getting its 401. This checks the cookie from the
    headers and answers without touching the body. Pure ASGI, so the accepted uploads
    still stream through unbuffered.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _needs_auth_before_body(scope):
            try:
                current_user(Request(scope))
            except HTTPException as exc:
                response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ---- OAuth flow ----


def _safe_next(next_path: str | None) -> str:
    """Only same-site paths, so /auth/login can't be used as an open redirect."""
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return "/"
    if "\\" in next_path or any(ord(c) < 32 for c in next_path):
        return "/"
    return next_path


def _frontend(path: str) -> str:
    return settings.FRONTEND_URL.rstrip("/") + path


@router.get("/login")
def login(next: str | None = None):
    if auth_disabled():
        return RedirectResponse(_frontend(_safe_next(next)))

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    params = {
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.AUTH_REDIRECT_URL,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    # A hint for Google's account picker only — the callback enforces the allowlist
    if len(settings.ALLOWED_DOMAINS) == 1:
        params["hd"] = settings.ALLOWED_DOMAINS[0]

    response = RedirectResponse(f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}")
    _set_cookie(
        response,
        STATE_COOKIE,
        _sign({
            "state": state,
            "nonce": nonce,
            "next": _safe_next(next),
            "exp": int(time.time()) + STATE_TTL_SECONDS,
        }),
        STATE_TTL_SECONDS,
    )
    return response


def _exchange_code(code: str) -> dict:
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": settings.GOOGLE_CLIENT_ID,
        "client_secret": settings.GOOGLE_CLIENT_SECRET,
        "redirect_uri": settings.AUTH_REDIRECT_URL,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as res:
        return json.loads(res.read())


def _id_token_claims(id_token: str) -> dict:
    parts = id_token.split(".")
    if len(parts) != 3:
        raise ValueError("Malformed ID token")
    return json.loads(_unb64(parts[1]))


def validate_claims(claims: dict, nonce: str) -> str | None:
    """Return why sign-in is refused, or None when the account is allowed."""
    if claims.get("iss") not in GOOGLE_ISSUERS:
        return "invalid"
    if claims.get("aud") != settings.GOOGLE_CLIENT_ID:
        return "invalid"
    if claims.get("exp", 0) < time.time():
        return "invalid"
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        return "invalid"
    if not claims.get("email") or claims.get("email_verified") is not True:
        return "unverified"
    if claims.get("hd") not in settings.ALLOWED_DOMAINS:
        return "domain"
    return None


@router.get("/callback")
def callback(request: Request, code: str | None = None, state: str | None = None,
             error: str | None = None):
    saved = _unsign(request.cookies.get(STATE_COOKIE))
    next_path = saved["next"] if saved else "/"

    def fail(reason: str) -> RedirectResponse:
        sep = "&" if "?" in next_path else "?"
        response = RedirectResponse(_frontend(f"{next_path}{sep}auth_error={reason}"))
        _clear_cookie(response, STATE_COOKIE)
        return response

    if error:  # user cancelled at Google, or Google refused
        return fail("cancelled")
    if not saved or not code or not state or not hmac.compare_digest(state, saved["state"]):
        return fail("expired")

    try:
        tokens = _exchange_code(code)
        claims = _id_token_claims(tokens["id_token"])
    except (urllib.error.URLError, KeyError, ValueError, json.JSONDecodeError):
        logger.exception("Google token exchange failed")
        return fail("failed")

    reason = validate_claims(claims, saved["nonce"])
    if reason:
        logger.info("Sign-in refused (%s) for %s", reason, claims.get("email"))
        return fail(reason)

    max_age = settings.SESSION_MAX_AGE_HOURS * 3600
    response = RedirectResponse(_frontend(next_path))
    _clear_cookie(response, STATE_COOKIE)
    _set_cookie(
        response,
        SESSION_COOKIE,
        _sign({
            "email": claims["email"],
            "name": claims.get("name") or claims["email"],
            "hd": claims["hd"],
            "exp": int(time.time()) + max_age,
        }),
        max_age,
    )
    return response


@router.get("/me")
def me(request: Request):
    user = current_user(request)
    return {"email": user.email, "name": user.name, "is_admin": user.is_admin}


@router.post("/logout", status_code=204)
def logout(request: Request):
    _check_origin(request)
    response = Response(status_code=204)
    _clear_cookie(response, SESSION_COOKIE)
    return response
