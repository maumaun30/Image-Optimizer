from functools import lru_cache
from pathlib import Path
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

    # CORS — comma-separated origins, e.g. https://your-app.vercel.app
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
