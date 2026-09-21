"""Inbound endpoint the MidPoint connector JAR posts lifecycle events to.

Secured with the shared MidPoint token (same secret both sides already hold), compared
in constant time. Distinct from the Teams Bot Framework auth used by /api/messages.
"""
import hmac
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.services.notify_service import handle_notification

logger = logging.getLogger("teams_bot.api.notify")
router = APIRouter()


class MidpointNotification(BaseModel):
    event: str = Field(min_length=1, max_length=64)
    recipientRole: Optional[str] = Field(default=None, max_length=32)
    recipientEmail: Optional[str] = Field(default=None, max_length=320)
    title: Optional[str] = Field(default=None, max_length=200)
    message: Optional[str] = Field(default=None, max_length=4000)
    status: Optional[str] = Field(default=None, max_length=32)
    caseOid: Optional[str] = Field(default=None, max_length=64)
    workItemId: Optional[str] = Field(default=None, max_length=64)
    requesterEmail: Optional[str] = Field(default=None, max_length=320)
    requesterName: Optional[str] = Field(default=None, max_length=200)
    requestedItem: Optional[str] = Field(default=None, max_length=200)


def _authorize(authorization: str | None) -> None:
    expected = settings.midpoint_api_token
    if not expected:
        raise HTTPException(status_code=503, detail="Notification channel is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing notification authorization token.")
    token = authorization.removeprefix("Bearer ").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid notification authorization token.")


@router.post("/notify")
async def midpoint_notify(request: Request, payload: MidpointNotification):
    _authorize(request.headers.get("Authorization"))
    # When the bot owns notifications (BOT_DIRECT_NOTIFY=true, the default while the midPoint push
    # notifier is unreliable), it already DMs the manager and requester itself with rich cards.
    # Any inbound midPoint-notifier event forwarded via the JAR is therefore a redundant duplicate
    # (and carries poorer data) — acknowledge and ignore it so recipients get exactly one card.
    # Set BOT_DIRECT_NOTIFY=false to make the notifier authoritative and process these instead.
    if settings.bot_direct_notify:
        logger.info("Ignoring inbound midPoint notification %s (bot_direct_notify owns delivery).", payload.event)
        return {"status": "ignored_bot_owns", "event": payload.event}
    try:
        result = await handle_notification(payload.model_dump())
    except Exception as exc:
        logger.error("Failed handling midPoint notification (%s): %s", payload.event, exc, exc_info=True)
        raise HTTPException(status_code=502, detail="Could not deliver the notification.")
    logger.info("midPoint notification %s → %s", payload.event, result.get("status"))
    return result
