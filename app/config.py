from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    APP_NAME: str = "Image Optimizer API"
    DEBUG: bool = False

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

    # Image processing
    DEFAULT_QUALITY: int = 85

    # CORS — comma-separated origins, e.g. https://your-app.vercel.app
    CORS_ORIGINS: list[str] = ["*"]

    model_config = {"env_file": ".env", "extra": "ignore"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
