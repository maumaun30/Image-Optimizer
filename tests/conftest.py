"""Test setup: SQLite instead of MySQL, temp dirs for files, no Celery broker.

Settings are read once at import, so the environment is configured here, before
anything under `app` is imported.
"""

import io
import os
import tempfile
import time
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="imageopt-tests-"))
os.environ.update(
    DEBUG="false",
    UPLOAD_DIR=str(_tmp / "uploads"),
    PROCESSED_DIR=str(_tmp / "processed"),
    GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com",
    GOOGLE_CLIENT_SECRET="test-secret",
    AUTH_REDIRECT_URL="https://api.corp.test/auth/callback",
    FRONTEND_URL="https://app.corp.test",
    SESSION_SECRET="s" * 48,
    SESSION_COOKIE_DOMAIN=".corp.test",
    ALLOWED_DOMAINS='["corp.test", "partner.test"]',
    ADMIN_EMAILS='["boss@corp.test"]',
    CORS_ORIGINS='["https://app.corp.test"]',
    AUTH_DISABLED="false",
    MIN_FREE_DISK_MB="0",
)

import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.database as database  # noqa: E402

# One shared in-memory connection, so the app, the worker tasks and the tests all
# see the same data
_engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
database.engine = _engine
database.SessionLocal = sessionmaker(bind=_engine, autoflush=False)

import app.workers.tasks as tasks  # noqa: E402
from app import auth  # noqa: E402
from app.config import settings  # noqa: E402
from app.models import Base  # noqa: E402

tasks.SessionLocal = database.SessionLocal

ORIGIN = "https://app.corp.test"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch):
    """Empty tables and file dirs per test; Celery dispatch becomes a no-op."""
    Base.metadata.drop_all(_engine)
    Base.metadata.create_all(_engine)
    for d in (settings.UPLOAD_DIR, settings.PROCESSED_DIR):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            f.unlink()
    for task in (tasks.process_image_task, tasks.process_pdf_task, tasks.process_video_task):
        monkeypatch.setattr(task, "delay", lambda *a, **k: None)
    yield


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app, base_url="https://api.corp.test") as c:
        yield c


@pytest.fixture
def db():
    session = database.SessionLocal()
    yield session
    session.close()


def session_cookie(email: str, hd: str = "corp.test", ttl: int = 3600) -> str:
    return auth._sign({"email": email, "name": email, "hd": hd, "exp": int(time.time()) + ttl})


def as_user(email: str, hd: str = "corp.test") -> dict:
    """Request headers for a signed-in browser on the frontend origin."""
    return {"cookie": f"{auth.SESSION_COOKIE}={session_cookie(email, hd)}", "origin": ORIGIN}


ALICE = "alice@corp.test"
BOB = "bob@corp.test"
BOSS = "boss@corp.test"


def png_bytes(size=(16, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, "PNG")
    return buf.getvalue()


def upload_image(client, email=ALICE, data=None, name="a.png", ctype="image/png"):
    return client.post(
        "/images/upload",
        files=[("files", (name, data if data is not None else png_bytes(), ctype))],
        headers=as_user(email),
    )
