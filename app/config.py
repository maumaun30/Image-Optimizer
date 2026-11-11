from functools import lru_cache
from pathlib import Path
from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Image Optimizer API"
    DEBUG: bool = False
    APP_PORT: int = 8000

    # Database
    DB_HOST: str = "localhost"
    DB_PORT: int = 3306
    DB_USER: str = "imageopt"
    DB_PASSWORD: str = "password"
    DB_NAME: str = "imageopt"

    @property
    def DATABASE_URL(self) -> str:
        return (
            f"mysql+pymysql://{self.DB_USER}:{self.DB_PASSWORD}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
        )

    # Redis / Celery
    REDIS_URL: str = "redis://localhost:6379/0"

    # Storage
    UPLOAD_DIR: Path = Path("uploads")
    PROCESSED_DIR: Path = Path("processed")
    MAX_FILE_SIZE_MB: int = 50

    # Cleanup
    AUTO_DELETE_HOURS: int = 24

    # Image processing — fallback quality (50 = max compression … 100 = lossless)
    DEFAULT_QUALITY: int = 85

    # Video — separate, much larger limit than images/PDFs
    MAX_VIDEO_SIZE_MB: int = 2048
    MAX_VIDEO_FILES: int = 5
    # Hard ceiling on a single encode; the worker kills ffmpeg past this
    VIDEO_TIME_LIMIT_SECONDS: int = 7200
    # Cap ffmpeg CPU use so one encode can't starve the box
    FFMPEG_THREADS: int = 2

    # Downloads stay available this long after the first one (dropped connections,
    # browser retries), then the cleanup task deletes the file
    DOWNLOAD_GRACE_MINUTES: int = 30

    # Abuse backstops — uploads get 503 past these (per-IP limits live in nginx)
    MAX_QUEUED_JOBS: int = 200
    MAX_QUEUED_VIDEOS: int = 20
    MIN_FREE_DISK_MB: int = 5120

    # Google sign-in (see app/auth.py). The API refuses to start with these unset.
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    # Must exactly match a redirect URI on the Google OAuth client
    AUTH_REDIRECT_URL: str = ""  # e.g. https://api.example.com/auth/callback
    FRONTEND_URL: str = ""  # e.g. https://app.example.com
    # 32+ random characters; rotating it signs everyone out
    SESSION_SECRET: str = ""
    # Parent domain shared by frontend and API, e.g. .example.com. Empty = API host only
    # (fine for localhost dev).
    SESSION_COOKIE_DOMAIN: str = ""
    SESSION_MAX_AGE_HOURS: int = 12
    # Google Workspace domains allowed to sign in (matched on the ID token's `hd` claim)
    ALLOWED_DOMAINS: list[str] = []
    # These accounts see every user's jobs in the /*/jobs listings
    ADMIN_EMAILS: list[str] = []
    # Local dev only, and only honoured with DEBUG=true: skip sign-in entirely
    AUTH_DISABLED: bool = False
    DEV_USER_EMAIL: str = "dev@localhost"

    @field_validator("ALLOWED_DOMAINS", "ADMIN_EMAILS")
    @classmethod
    def _lowercase(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v.strip()]

    # CORS — comma-separated origins, e.g. https://your-app.vercel.app
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
