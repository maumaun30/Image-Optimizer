import os
from datetime import datetime, timedelta

from app.config import settings
from app.models import ImageJob, JobStatus, PdfJob
from app.workers import tasks

from .conftest import ALICE, BOB, BOSS, ORIGIN, as_user, png_bytes, upload_image


# ---- uploads ----


def test_anonymous_upload_is_refused_before_the_body_is_read(client):
    r = client.post("/video/upload", files=[("files", ("v.mp4", b"x" * 1024, "video/mp4"))])
    assert r.status_code == 401
    assert not any(settings.UPLOAD_DIR.iterdir())


def test_upload_records_the_owner(client, db):
    r = upload_image(client)
    assert r.status_code == 202
    job = db.get(ImageJob, r.json()["jobs"][0]["id"])
    assert job.owner_email == ALICE


def test_spoofed_image_is_rejected(client):
    assert upload_image(client, data=b"not an image").status_code == 400
    assert not any(settings.UPLOAD_DIR.iterdir())


def test_non_pdf_is_rejected(client):
    r = client.post(
        "/pdf/upload", files=[("files", ("a.pdf", b"hello", "application/pdf"))], headers=as_user(ALICE)
    )
    assert r.status_code == 400


def test_oversize_is_413(monkeypatch, client):
    monkeypatch.setattr(settings, "MAX_FILE_SIZE_MB", 0)
    assert upload_image(client).status_code == 413


def test_one_bad_file_rejects_the_batch_and_cleans_up(client):
    r = client.post(
        "/images/upload",
        files=[
            ("files", ("ok.png", png_bytes(), "image/png")),
            ("files", ("bad.png", b"junk", "image/png")),
        ],
        headers=as_user(ALICE),
    )
    assert r.status_code == 400
    assert not any(settings.UPLOAD_DIR.iterdir())


def test_full_queue_is_503(monkeypatch, client):
    monkeypatch.setattr(settings, "MAX_QUEUED_JOBS", 0)
    assert upload_image(client).status_code == 503


def test_low_disk_is_503(monkeypatch, client):
    monkeypatch.setattr(settings, "MIN_FREE_DISK_MB", 10**12)
    assert upload_image(client).status_code == 503


# ---- ownership ----


def test_jobs_are_private_to_their_owner(client):
    job_id = upload_image(client).json()["jobs"][0]["id"]
    assert client.get(f"/images/status/{job_id}", headers=as_user(ALICE)).status_code == 200
    # Someone else's job looks like it doesn't exist
    assert client.get(f"/images/status/{job_id}", headers=as_user(BOB)).status_code == 404
    assert client.get(f"/images/download/{job_id}", headers=as_user(BOB)).status_code == 404
    assert client.get(f"/images/status/{job_id}", headers=as_user(BOSS)).status_code == 200


def test_listings(client):
    upload_image(client)
    mine = lambda email, **p: client.get("/images/jobs", params=p, headers=as_user(email)).json()["total"]  # noqa: E731
    assert mine(ALICE) == 1
    assert mine(BOB) == 0
    assert mine(BOB, all="true") == 0  # not an admin
    assert mine(BOSS) == 0  # admins see their own by default
    assert mine(BOSS, all="true") == 1
    assert client.get("/images/jobs").status_code == 401


def test_ownerless_legacy_jobs_are_admin_only(client, db):
    db.add(ImageJob(id="legacy", original_filename="old.png", output_format="webp", quality=85))
    db.commit()
    assert client.get("/images/status/legacy", headers=as_user(ALICE)).status_code == 404
    assert client.get("/images/status/legacy", headers=as_user(BOSS)).status_code == 200


# ---- processing, download grace window, cleanup ----


def _ready_job(client):
    job_id = upload_image(client).json()["jobs"][0]["id"]
    tasks.process_image_task.run(job_id)
    return job_id


def test_download_survives_retries_then_expires(client, db):
    job_id = _ready_job(client)
    first = client.get(f"/images/download/{job_id}", headers=as_user(ALICE))
    again = client.get(f"/images/download/{job_id}", headers=as_user(ALICE))
    assert first.status_code == again.status_code == 200
    assert first.content == again.content and len(first.content) > 0

    job = db.get(ImageJob, job_id)
    path = job.processed_path
    job.downloaded_at = datetime.utcnow() - timedelta(minutes=settings.DOWNLOAD_GRACE_MINUTES + 1)
    db.commit()
    assert client.get(f"/images/download/{job_id}", headers=as_user(ALICE)).status_code == 410

    tasks.cleanup_expired_jobs()
    db.expire_all()
    assert not os.path.exists(path)
    assert db.get(ImageJob, job_id).processed_path is None
    assert db.get(ImageJob, job_id).status == JobStatus.DOWNLOADED


def test_corrupt_image_fails_without_retrying(db):
    bad = settings.UPLOAD_DIR / "bad.png"
    bad.write_bytes(b"junk")
    db.add(ImageJob(id="bad", original_filename="bad.png", original_path=str(bad),
                    status=JobStatus.PENDING, output_format="webp", quality=85))
    db.commit()
    tasks.process_image_task.run("bad")  # would raise Retry if it tried again
    db.expire_all()
    assert db.get(ImageJob, "bad").status == JobStatus.FAILED


def test_corrupt_pdf_fails_without_retrying(db):
    bad = settings.UPLOAD_DIR / "bad.pdf"
    bad.write_bytes(b"%PDF-1.4 garbage")
    db.add(PdfJob(id="badpdf", original_filename="bad.pdf", original_path=str(bad),
                  status=JobStatus.PENDING, compression_level="lossless"))
    db.commit()
    tasks.process_pdf_task.run("badpdf")
    db.expire_all()
    assert db.get(PdfJob, "badpdf").status == JobStatus.FAILED


def test_health_is_public(client):
    assert client.get("/health", headers={"origin": ORIGIN}).json() == {"status": "ok"}
