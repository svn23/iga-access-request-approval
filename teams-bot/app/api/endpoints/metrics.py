"""Operational metrics for conversation-security events (bearer-protected)."""
import logging

from fastapi import APIRouter, HTTPException, Request

from app.services.bot_auth import validate_bot_framework_authorization
from app.services import metrics_service

logger = logging.getLogger("teams_bot.api.metrics")
router = APIRouter()


@router.get("/metrics")
async def metrics(request: Request):
    await validate_bot_framework_authorization(request.headers.get("Authorization"))
    try:
        return metrics_service.snapshot()
    except Exception as exc:
        logger.error("metrics snapshot failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not read metrics.")
