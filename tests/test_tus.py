import base64
import os
from datetime import datetime, timedelta

import pytest

import app.routers.tus as tus
from app.config import settings
from app.models import TusUpload, VideoJob
from app.workers import tasks

from .conftest import ALICE, BOB, as_user

TUS = {"Tus-Resumable": "1.0.0"}
PATCH = {**TUS, "Content-Type": "application/offset+octet-stream"}


def meta(**kv) -> str:
    return ",".join(f"{k} {base64.b64encode(str(v).encode()).decode()}" for k, v in kv.items())


@pytest.fixture(autouse=True)
def ffmpeg_present(monkeypatch):
    monkeypatch.setattr(tus, "ffmpeg_bin", lambda: "ffmpeg")
    monkeypatch.setattr(tus, "ffprobe_bin", lambda: "ffprobe")


def create(client, length, email=ALICE, **m):
    m.setdefault("filename", "clip.mp4")
    m.setdefault("filetype", "video/mp4")
    return client.post(
        "/video/tus/",
        headers={**as_user(email), **TUS, "Upload-Length": str(length), "Upload-Metadata": meta(**m)},
    )


def patch(client, location, offset, data, email=ALICE):
    return client.patch(
        location, content=data, headers={**as_user(email), **PATCH, "Upload-Offset": str(offset)}
    )


def offset_of(client, location, email=ALICE):
    r = client.head(location, headers={**as_user(email), **TUS})
    return r.status_code, r.headers.get("upload-offset")


def test_full_upload_becomes_a_queued_job(client, db):
    data = os.urandom(3000)
    r = create(client, len(data), codec="vp9", mute="true", width="1280")
    assert r.status_code == 201
    loc = r.headers["location"]
    assert loc.startswith("/video/tus/")

    assert patch(client, loc, 0, data[:1000]).status_code == 204
    assert offset_of(client, loc) == (200, "1000")
    r = patch(client, loc, 1000, data[1000:])
    assert r.status_code == 204 and r.headers["upload-offset"] == "3000"

    body = client.get(loc, headers=as_user(ALICE)).json()
    job = db.get(VideoJob, body["job"]["id"])
    assert job.owner_email == ALICE and job.codec.value == "vp9" and job.mute and job.target_width == 1280
    with open(job.original_path, "rb") as f:
        assert f.read() == data


def test_wrong_offset_is_409_and_keeps_progress(client):
    loc = create(client, 100).headers["location"]
    patch(client, loc, 0, b"x" * 40)
    r = patch(client, loc, 0, b"y" * 40)  # retried chunk
    assert r.status_code == 409
    assert offset_of(client, loc) == (200, "40")


def test_chunk_over_the_limit_is_rejected_whole(monkeypatch, client):
    monkeypatch.setattr(settings, "TUS_MAX_CHUNK_MB", 0)
    loc = create(client, 100).headers["location"]
    assert patch(client, loc, 0, b"x" * 10).status_code == 413
    assert offset_of(client, loc) == (200, "0")


def test_cannot_write_past_the_declared_length(client):
    loc = create(client, 10).headers["location"]
    assert patch(client, loc, 0, b"x" * 11).status_code == 413


def test_complete_upload_rejects_more_data(client):
    loc = create(client, 5).headers["location"]
    patch(client, loc, 0, b"12345")
    assert patch(client, loc, 5, b"6").status_code == 409


@pytest.mark.parametrize(
    "length,m,status",
    [
        (0, {}, 400),
        (10**13, {}, 413),
        (10, {"filename": "a.exe", "filetype": "application/x-msdownload"}, 400),
        (10, {"codec": "h265"}, 400),
        (10, {"width": "99999"}, 400),
    ],
)
def test_creation_validation(client, length, m, status):
    assert create(client, length, **m).status_code == status


def test_requires_tus_header(client):
    r = client.post("/video/tus/", headers={**as_user(ALICE), "Upload-Length": "5"})
    assert r.status_code == 412


def test_anonymous_uploads_are_refused(client):
    assert client.post("/video/tus/", headers={**TUS, "Upload-Length": "5"}).status_code == 401
    loc = create(client, 5).headers["location"]
    r = client.patch(loc, content=b"12345", headers={**PATCH, "Upload-Offset": "0"})
    assert r.status_code == 401


def test_uploads_are_private(client):
    loc = create(client, 5).headers["location"]
    assert offset_of(client, loc, email=BOB)[0] == 404
    assert patch(client, loc, 0, b"12345", email=BOB).status_code == 404
    assert client.get(loc, headers=as_user(BOB)).status_code == 404


def test_open_upload_limit(monkeypatch, client):
    monkeypatch.setattr(settings, "TUS_MAX_OPEN_PER_USER", 1)
    assert create(client, 5).status_code == 201
    assert create(client, 5).status_code == 429
    assert create(client, 5, email=BOB).status_code == 201


def test_disk_is_reserved_for_the_whole_file(monkeypatch, client):
    monkeypatch.setattr(settings, "MIN_FREE_DISK_MB", 1)
    free = __import__("shutil").disk_usage(settings.UPLOAD_DIR).free
    assert create(client, free).status_code == 507


def test_cancel_deletes_the_partial_file(client, db):
    loc = create(client, 10).headers["location"]
    patch(client, loc, 0, b"12345")
    path = db.query(TusUpload).one().path
    assert client.delete(loc, headers={**as_user(ALICE), **TUS}).status_code == 204
    assert not os.path.exists(path)
    assert offset_of(client, loc)[0] == 404


def test_cleanup_drops_abandoned_uploads(client, db):
    loc = create(client, 10).headers["location"]
    patch(client, loc, 0, b"12345")
    upload = db.query(TusUpload).one()
    path = upload.path
    upload.updated_at = datetime.utcnow() - timedelta(hours=settings.TUS_EXPIRE_HOURS + 1)
    db.commit()
    tasks.cleanup_expired_jobs()
    db.expire_all()
    assert db.query(TusUpload).count() == 0
    assert not os.path.exists(path)


def test_duplicate_session_cookies_any_valid_one_works(client):
    """After a cookie-domain move the browser sends an old and a new cookie."""
    from app import auth

    from .conftest import session_cookie

    stale = session_cookie(ALICE, ttl=-1)
    good = session_cookie(ALICE)
    for cookie in (f"{auth.SESSION_COOKIE}={stale}; {auth.SESSION_COOKIE}={good}",
                   f"{auth.SESSION_COOKIE}={good}; {auth.SESSION_COOKIE}={stale}"):
        assert client.get("/auth/me", headers={"cookie": cookie}).status_code == 200


def test_cors_exposes_tus_headers(client):
    loc = create(client, 5).headers["location"]
    r = client.head(loc, headers={**as_user(ALICE), **TUS})
    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "upload-offset" in exposed and "location" in exposed
