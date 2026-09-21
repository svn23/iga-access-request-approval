# pyrefly: ignore [missing-import]
from fastapi import APIRouter
from app.api.endpoints.health import router as health_router
from app.api.endpoints.bot import router as bot_router
from app.api.endpoints.requests import router as requests_router
from app.api.endpoints.notify import router as notify_router
from app.api.endpoints.metrics import router as metrics_router

api_router = APIRouter()

# Include sub-routers with prefixes (using /api prefix or registering it globally)
api_router.include_router(health_router, prefix="/api")
api_router.include_router(bot_router, prefix="/api")
api_router.include_router(requests_router, prefix="/api")
api_router.include_router(notify_router, prefix="/api")
api_router.include_router(metrics_router, prefix="/api")
