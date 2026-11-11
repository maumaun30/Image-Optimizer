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
4. `GET /images/download/{id}` — streams file, marks job `DOWNLOADED`; the file stays re-downloadable for `DOWNLOAD_GRACE_MINUTES` (dropped connections, browser retries)
5. Celery Beat runs `cleanup_expired_jobs` every 10 min: deletes downloaded files past the grace window, and purges stale files older than `AUTO_DELETE_HOURS`

## Local dev

```bash
cp .env.example .env          # fill in values
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Start dependencies (Docker)
# db publishes 127.0.0.1:3307 (not 3306) so it can't collide with a local
# MySQL/MariaDB install — set DB_PORT=3307 in .env for host-side runs.
docker compose up db redis -d

# Run migrations
alembic upgrade head
# python3 -m alembic upgrade head <--> for mac cli

# Start API
uvicorn app.main:app --reload

# Start worker (separate terminal)
celery -A app.workers.celery_app worker --loglevel=info

# Start video worker (separate terminal — dedicated queue, one encode at a time)
celery -A app.workers.celery_app worker --loglevel=info -Q video --concurrency=1

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
| POST | `/images/upload` | Upload 1-20 images; query params: `format` (webp/avif/original), `width` (int), `quality` (50-100, default 85; 100 = lossless) |
| GET | `/images/status/{id}` | Poll job status |
| GET | `/images/download/{id}` | Download processed image (re-downloadable for `DOWNLOAD_GRACE_MINUTES`, then deleted) |
| GET | `/images/jobs` | List your jobs (query: `limit`, `offset`; admins add `all=true` for everyone's) |
| POST | `/pdf/upload` | Upload 1-20 PDFs; query param: `level` (screen/ebook/printer/lossless) |
| GET | `/pdf/status/{id}` | Poll PDF job status |
| GET | `/pdf/download/{id}` | Download compressed PDF (same grace window as images) |
| GET | `/pdf/jobs` | List your PDF jobs (same query params as images) |
| POST | `/video/upload` | Upload 1-5 videos; query params: `preset` (low/balanced/high), `codec` (h264/vp9/av1), `width` (int), `mute` (bool) |
| GET | `/video/status/{id}` | Poll video job status (includes `progress_percent`) |
| GET | `/video/download/{id}` | Download compressed video (same grace window as images) |
| GET | `/video/jobs` | List your video jobs (same query params as images) |
| GET | `/auth/login` | Start Google sign-in; `next` = frontend path to return to |
| GET | `/auth/callback` | Google redirect target; sets the session cookie |
| GET | `/auth/me` | Signed-in user (`email`, `name`, `is_admin`) or 401 |
| POST | `/auth/logout` | Clear the session cookie |
| GET | `/health` | Health check (public) |

### Authentication

Every endpoint except `/health` and `/auth/*` needs a signed-in Google Workspace account whose `hd` (hosted domain) claim is in `ALLOWED_DOMAINS`; personal Gmail accounts have no `hd` and are always refused. `app/auth.py` runs the OAuth code flow itself and keeps the session in a signed httpOnly cookie on `SESSION_COOKIE_DOMAIN`, so the frontend, API and upload hosts must share that parent domain. No extra packages: HMAC-SHA256 signs cookies, and the ID token comes straight from Google's token endpoint over TLS.

- Jobs record `owner_email`. Status, download and listings only show your own jobs (404 otherwise); `ADMIN_EMAILS` can list everyone's with `?all=true`. Jobs from before migration 005 have no owner and are visible to admins only.
- `UploadAuthMiddleware` rejects anonymous `POST */upload` before the body is read (FastAPI parses multipart bodies before dependencies run); nginx streams upload bodies for the same reason.
- POSTs with an `Origin` outside `CORS_ORIGINS` get 403 — SameSite=Lax alone lets sibling subdomains through.
- The API refuses to start with auth settings missing or `CORS_ORIGINS=["*"]`. For local dev without Google, set `DEBUG=true` and `AUTH_DISABLED=true` (everything runs as an admin dev user).

### Abuse limits

- **Per-IP upload rate limits** live in nginx (`limit_req` zones in `nginx/imageopt-http.conf`, shared by both hosts; CORS preflights aren't counted). The API host sits behind Cloudflare, so its snippet includes `cloudflare-realip.conf` — without it every visitor shares the few Cloudflare edge IPs.
- **Capacity backstop** in the app: uploads get 503 when `MAX_QUEUED_JOBS` / `MAX_QUEUED_VIDEOS` jobs are pending or processing, or free disk falls below `MIN_FREE_DISK_MB`.
- **Uploads stream to disk** for every type and are sniffed from their bytes (Pillow for images, `%PDF-` header for PDFs; ffprobe in the worker for video). Corrupt inputs fail without retries.

### PDF compression

`POST /pdf/upload` queues `process_pdf_task`, which compresses via **Ghostscript** when the `gs` binary is on PATH (downsamples embedded images per preset — `screen`/`ebook`/`printer`), and otherwise falls back to **pikepdf** lossless structural compression. The `lossless` level always uses pikepdf. PDF jobs live in the `pdf_jobs` table and share the same status flow, auto-delete, and hourly cleanup as image jobs. The Docker image installs `ghostscript`; on bare-metal, `apt-get install ghostscript` enables the better compression path.

### Video compression

`POST /video/upload` queues `process_video_task`, which compresses via **ffmpeg**. Unlike PDFs there is **no fallback encoder** — without `ffmpeg` and `ffprobe` on PATH the endpoint returns 503. The Docker image installs `ffmpeg`; on Ubuntu bare-metal, `apt-get install ffmpeg`; on macOS, `brew install ffmpeg`.

Differences from the image/PDF pipelines:

- **Uploads stream to disk** in 4 MB chunks (a 2 GB file must never be read into memory). Limit is `MAX_VIDEO_SIZE_MB` (default 2048), separate from `MAX_FILE_SIZE_MB`.
- **Progress tracking** — `video_jobs.progress_percent` is written by the worker every ~5 %, parsed from `ffmpeg -progress pipe:1`. Poll `/video/status/{id}`.
- **Dedicated queue** — video tasks route to the `video` Celery queue, consumed by a separate worker at `--concurrency=1` so encodes can't starve image/PDF jobs. Run it with `celery -A app.workers.celery_app worker -Q video --concurrency=1` (systemd: `imageopt-video-worker`).
- **No blind retries** — ffmpeg failures (bad input, missing encoder, time limit) fail the job immediately; only unexpected errors retry, at most once. `VIDEO_TIME_LIMIT_SECONDS` (default 7200) kills a runaway encode.
- Codecs: `h264` → `.mp4` (default, universal, fast), `vp9` → `.webm` (smaller, slower), `av1` → `.mp4` (smallest, much slower; needs a `libsvtav1`-enabled ffmpeg build).
- Sizes use `BIGINT` — 2 GB overflows MySQL's signed `INT`.

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

# nginx — see "Nginx (Cloud Panel)" below
mkdir -p /etc/nginx/snippets
cp nginx/imageopt-http.conf /etc/nginx/sites-enabled/00-imageopt-shared.conf
cp nginx/cloudflare-realip.conf nginx/imageopt-api.locations.conf \
   nginx/imageopt-upload.locations.conf /etc/nginx/snippets/
nginx -t && systemctl reload nginx

# Install systemd services
cp deploy/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable imageopt-api imageopt-worker imageopt-video-worker imageopt-beat
systemctl start imageopt-api imageopt-worker imageopt-video-worker imageopt-beat
```

## Nginx (Cloud Panel)

Two Cloud Panel sites front the same API process; Cloud Panel owns each site's `listen`, `server_name` and SSL lines, so the repo ships include files rather than whole vhosts:

| Host | Cloudflare | Serves | Include in its vhost |
|---|---|---|---|
| `image-optimizer.quailshack.com` | proxied (orange) | the whole API | `include /etc/nginx/snippets/imageopt-api.locations.conf;` |
| `upload.quailshack.com` | DNS only (grey) | `POST /video/upload` only — bypasses Cloudflare's 100 MB body cap | `include /etc/nginx/snippets/imageopt-upload.locations.conf;` |

- `nginx/imageopt-http.conf` → `/etc/nginx/sites-enabled/00-imageopt-shared.conf`: the `imageopt_api` upstream and the rate-limit zones. http-level, so it must exist exactly once; Cloud Panel's `nginx.conf` includes only `sites-enabled/*.conf` (no `conf.d`). `/etc/nginx/snippets/` doesn't exist on Cloud Panel — create it.
- In each site's vhost (Cloud Panel → Sites → site → Vhost), replace the site's own `location / { … }` proxy block with the include line above. Keep everything Cloud Panel generated for listen/SSL.
- `upstream imageopt_api` must point at the API's `APP_PORT`.
- The deploy workflow does not touch nginx; re-copy the files after changing them.

## Deploy user

`SSH_USER` is the Cloud Panel site user `quailshack-image-optimizer` (it owns the code, so `git pull`, `pip` and `alembic` need no sudo). It may run exactly the two restart commands the workflow uses, nothing else — `/etc/sudoers.d/imageopt-deploy`:

```
quailshack-image-optimizer ALL=(root) NOPASSWD: /usr/bin/systemctl restart imageopt-worker imageopt-video-worker imageopt-beat, /usr/bin/systemctl reload-or-restart imageopt-api
```

Sudoers matches arguments exactly, so changing those lines in `deploy.yml` means changing this file too. No wildcards: `imageopt-*` would also match extra unit names appended after it.

## GitHub Actions secrets required

| Secret | Value |
|---|---|
| `SSH_HOST` | Server IP or hostname |
| `SSH_USER` | `quailshack-image-optimizer` (see Deploy user) |
| `SSH_PRIVATE_KEY` | Private key contents (the deploy key) |
| `SSH_PORT` | SSH port (default 22) |
| `DEPLOY_PATH` | `/home/quailshack-image-optimizer/htdocs/image-optimizer.quailshack.com` |

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
