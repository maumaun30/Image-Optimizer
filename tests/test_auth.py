import json
import time
import urllib.parse

import pytest

from app import auth
from app.config import settings

from .conftest import ALICE, BOSS, ORIGIN, as_user, session_cookie


def claims(**over):
    base = {
        "iss": "https://accounts.google.com",
        "aud": settings.GOOGLE_CLIENT_ID,
        "exp": int(time.time()) + 60,
        "nonce": "n1",
        "email": ALICE,
        "email_verified": True,
        "hd": "corp.test",
    }
    base.update(over)
    return {k: v for k, v in base.items() if v is not None}


# ---- config ----


def test_allowlists_are_lowercased():
    assert settings.ALLOWED_DOMAINS == ["corp.test", "partner.test"]
    assert settings.ADMIN_EMAILS == ["boss@corp.test"]


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("SESSION_SECRET", "", "SESSION_SECRET"),
        ("SESSION_SECRET", "short", "32 characters"),
        ("GOOGLE_CLIENT_ID", "", "GOOGLE_CLIENT_ID"),
        ("ALLOWED_DOMAINS", [], "ALLOWED_DOMAINS"),
        ("CORS_ORIGINS", ["*"], "CORS_ORIGINS"),
    ],
)
def test_refuses_to_start_half_configured(monkeypatch, field, value, message):
    monkeypatch.setattr(settings, field, value)
    with pytest.raises(RuntimeError, match=message):
        auth.check_auth_config()


def test_auth_disabled_needs_debug(monkeypatch, client):
    monkeypatch.setattr(settings, "AUTH_DISABLED", True)
    assert client.get("/auth/me").status_code == 401
    monkeypatch.setattr(settings, "DEBUG", True)
    assert client.get("/auth/me").json()["is_admin"] is True


# ---- signed cookies ----


def test_forged_and_expired_sessions_are_rejected(client):
    tok = session_cookie(ALICE)
    body, sig = tok.rsplit(".", 1)
    forged = auth._b64(json.dumps({"email": BOSS, "hd": "corp.test", "exp": 9e9}).encode()) + "." + sig
    for bad in (forged, "garbage", "a.b", session_cookie(ALICE, ttl=-1)):
        r = client.get("/auth/me", headers={"cookie": f"{auth.SESSION_COOKIE}={bad}"})
        assert r.status_code == 401


def test_me(client):
    assert client.get("/auth/me").status_code == 401
    r = client.get("/auth/me", headers=as_user(ALICE))
    assert r.json() == {"email": ALICE, "name": ALICE, "is_admin": False}
    assert client.get("/auth/me", headers=as_user("Boss@corp.test")).json()["is_admin"] is True


def test_removing_a_domain_locks_its_sessions_out(monkeypatch, client):
    headers = as_user("x@partner.test", hd="partner.test")
    assert client.get("/auth/me", headers=headers).status_code == 200
    monkeypatch.setattr(settings, "ALLOWED_DOMAINS", ["corp.test"])
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_posts_from_foreign_origins_are_refused(client):
    headers = {**as_user(ALICE), "origin": "https://evil.corp.test"}
    r = client.post("/images/upload", files=[("files", ("a.png", b"x", "image/png"))], headers=headers)
    assert r.status_code == 403
    assert client.post("/auth/logout", headers={"origin": "https://evil.test"}).status_code == 403


# ---- ID token claims ----


@pytest.mark.parametrize(
    "over,expected",
    [
        ({}, None),
        ({"hd": "partner.test"}, None),
        ({"hd": None, "email": "a@gmail.com"}, "domain"),
        ({"hd": "evil.test"}, "domain"),
        ({"hd": "corp.test.evil.test"}, "domain"),
        ({"aud": "someone-else"}, "invalid"),
        ({"iss": "https://evil.test"}, "invalid"),
        ({"exp": 1}, "invalid"),
        ({"nonce": "other"}, "invalid"),
        ({"email_verified": False}, "unverified"),
    ],
)
def test_validate_claims(over, expected):
    assert auth.validate_claims(claims(**over), "n1") == expected


@pytest.mark.parametrize("bad", ["//evil.test", "https://evil.test", "/\\evil.test", "evil", "/\nx", None])
def test_next_cannot_leave_the_frontend(bad):
    assert auth._safe_next(bad) == "/"


def test_next_keeps_same_site_paths():
    assert auth._safe_next("/video?x=1") == "/video?x=1"


# ---- full OAuth round trip ----


def _login(client, next_path="/pdf"):
    r = client.get("/auth/login", params={"next": next_path}, follow_redirects=False)
    assert r.status_code == 307
    q = urllib.parse.parse_qs(urllib.parse.urlparse(r.headers["location"]).query)
    state_cookie = r.cookies.get(auth.STATE_COOKIE)
    assert state_cookie
    return q, state_cookie


def _callback(client, state_cookie, **params):
    return client.get(
        "/auth/callback",
        params=params,
        headers={"cookie": f"{auth.STATE_COOKIE}={state_cookie}"},
        follow_redirects=False,
    )


def _token_response(c):
    payload = auth._b64(json.dumps(c).encode())
    return {"id_token": f"header.{payload}.sig"}


def test_login_redirects_to_google(client):
    q, _ = _login(client)
    assert q["client_id"] == [settings.GOOGLE_CLIENT_ID]
    assert q["redirect_uri"] == [settings.AUTH_REDIRECT_URL]
    assert "hd" not in q  # two allowed domains: no single hint


def test_callback_signs_in_and_returns_to_next(monkeypatch, client):
    q, state = _login(client)
    monkeypatch.setattr(auth, "_exchange_code", lambda code: _token_response(claims(nonce=q["nonce"][0])))
    r = _callback(client, state, code="c", state=q["state"][0])
    assert r.headers["location"] == "https://app.corp.test/pdf"
    set_cookie = r.headers.get_list("set-cookie") if hasattr(r.headers, "get_list") else r.headers["set-cookie"]
    joined = " ".join(set_cookie) if isinstance(set_cookie, list) else set_cookie
    assert "HttpOnly" in joined and "Secure" in joined and "Domain=.corp.test" in joined
    session = r.cookies.get(auth.SESSION_COOKIE)
    me = client.get("/auth/me", headers={"cookie": f"{auth.SESSION_COOKIE}={session}"})
    assert me.json()["email"] == ALICE


@pytest.mark.parametrize(
    "token_claims,params,error",
    [
        ({"hd": None, "email": "a@gmail.com"}, {}, "domain"),
        ({}, {"state": "tampered"}, "expired"),
        ({}, {"error": "access_denied"}, "cancelled"),
    ],
)
def test_callback_refusals(monkeypatch, client, token_claims, params, error):
    q, state = _login(client)
    monkeypatch.setattr(
        auth, "_exchange_code", lambda code: _token_response(claims(nonce=q["nonce"][0], **token_claims))
    )
    query = {"code": "c", "state": q["state"][0], **params}
    r = _callback(client, state, **query)
    assert f"auth_error={error}" in r.headers["location"]
    assert auth.SESSION_COOKIE not in r.cookies


def test_logout_clears_the_session(client):
    r = client.post("/auth/logout", headers={"origin": ORIGIN})
    assert r.status_code == 204
    assert auth.SESSION_COOKIE in r.headers["set-cookie"]


def test_cors_allows_the_frontend_with_credentials(client):
    r = client.options(
        "/video/upload",
        headers={"origin": ORIGIN, "access-control-request-method": "POST"},
    )
    assert r.headers["access-control-allow-origin"] == ORIGIN
    assert r.headers["access-control-allow-credentials"] == "true"
