import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import auth
from app.config import settings
from app.routers import images, pdf, video


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    auth.check_auth_config()
    if "*" in settings.CORS_ORIGINS and not settings.DEBUG:
        logger.warning(
            "CORS_ORIGINS allows any origin. Set it to the frontend URL in production."
        )
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    settings.PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description="Fast image optimization API with queue processing",
    version="1.0.0",
    lifespan=lifespan,
)

# Added before CORS so CORS wraps it and its 401s still carry CORS headers
app.add_middleware(auth.UploadAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    # The session cookie must ride along on cross-origin fetch/XHR from the frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(images.router)
app.include_router(pdf.router)
app.include_router(video.router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}
