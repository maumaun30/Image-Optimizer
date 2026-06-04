# Image Optimizer API

FastAPI service that accepts image uploads, queues optimization jobs via Celery, and auto-deletes files after download.

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| Queue | Celery + Redis |
| DB | MySQL 8 + SQLAlchemy 2 + Alembic |
| Image processing | Pillow 10+ |
| Reverse proxy | Nginx |

## Key flow

1. `POST /images/upload` — saves files to `uploads/`, creates `ImageJob` rows, dispatches Celery tasks
2. Celery worker resizes + converts → saves to `processed/`, deletes original upload, marks job `READY`
3. `GET /images/status/{id}` — poll until `READY`
4. `GET /images/download/{id}` — streams file, deletes processed file after response, marks job `DOWNLOADED`
5. Celery Beat runs hourly `cleanup_expired_jobs` to purge stale files older than `AUTO_DELETE_HOURS`

## Local dev

```bash
cp .env.example .env          # fill in values
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Start dependencies (Docker)
docker compose up db redis -d

# Run migrations
alembic upgrade head

# Start API
uvicorn app.main:app --reload

# Start worker (separate terminal)
celery -A app.workers.celery_app worker --loglevel=info

# Start beat scheduler (separate terminal)
celery -A app.workers.celery_app beat --loglevel=info
```

Or run everything with Docker:

```bash
docker compose up --build
```

## API endpoints

| Method | Path | Description |
|---|---|---|
| POST | `/images/upload` | Upload 1-20 images; query params: `format` (webp/avif/original), `width` (int) |
| GET | `/images/status/{id}` | Poll job status |
| GET | `/images/download/{id}` | Download processed image (one-time, auto-deletes) |
| GET | `/images/jobs` | List all jobs (query: `limit`, `offset`) |
| POST | `/pdf/upload` | Upload 1-20 PDFs; query param: `level` (screen/ebook/printer/lossless) |
| GET | `/pdf/status/{id}` | Poll PDF job status |
| GET | `/pdf/download/{id}` | Download compressed PDF (one-time, auto-deletes) |
| GET | `/pdf/jobs` | List all PDF jobs (query: `limit`, `offset`) |
| GET | `/health` | Health check |

### PDF compression

`POST /pdf/upload` queues `process_pdf_task`, which compresses via **Ghostscript** when the `gs` binary is on PATH (downsamples embedded images per preset — `screen`/`ebook`/`printer`), and otherwise falls back to **pikepdf** lossless structural compression. The `lossless` level always uses pikepdf. PDF jobs live in the `pdf_jobs` table and share the same status flow, auto-delete, and hourly cleanup as image jobs. The Docker image installs `ghostscript`; on bare-metal, `apt-get install ghostscript` enables the better compression path.

## Deployment (Cloud Panel + Nginx)

```bash
# On server — first-time setup (Cloud Panel site root)
git clone <repo> /home/quailshack-image-optimizer/htdocs/image-optimizer.quailshack.com
cd /home/quailshack-image-optimizer/htdocs/image-optimizer.quailshack.com
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in production values
alembic upgrade head

# Copy nginx config
cp nginx/nginx.conf /etc/nginx/sites-available/imageopt
ln -s /etc/nginx/sites-available/imageopt /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx

# Install systemd services
cp deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable imageopt-api imageopt-worker imageopt-beat
systemctl start imageopt-api imageopt-worker imageopt-beat
```

## GitHub Actions secrets required

| Secret | Value |
|---|---|
| `SSH_HOST` | Server IP or hostname |
| `SSH_USER` | SSH username (e.g. `www-data` or `deploy`) |
| `SSH_PRIVATE_KEY` | Private key contents (the deploy key) |
| `SSH_PORT` | SSH port (default 22) |

## AVIF support

Pillow 10+ wheels on Linux include libavif. On Ubuntu bare-metal, if AVIF save fails:

```bash
apt-get install libavif-dev
pip install --no-binary Pillow Pillow
```

## Environment variables

See `.env.example` for all variables. Key ones:

- `DB_*` — MySQL connection
- `REDIS_URL` — Redis for Celery
- `MAX_FILE_SIZE_MB` — per-file upload limit (default 50)
- `AUTO_DELETE_HOURS` — hours before undownloaded files are purged (default 24)
- `DEFAULT_QUALITY` — Pillow save quality 1-95 (default 85)
