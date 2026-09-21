import asyncio
import logging
# pyrefly: ignore [missing-import]
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse
from app.services.bot_handler import process_activity
from app.config import settings
from app.services.bot_auth import validate_bot_framework_authorization

logger = logging.getLogger("teams_bot.api.bot")
router = APIRouter()

# Keep strong references so background tasks aren't garbage-collected mid-flight.
_background_tasks: set = set()


async def _run_activity_bg(activity: dict) -> None:
    """Process an activity off the request thread; all user-facing output is sent proactively."""
    try:
        await process_activity(activity)
    except Exception:  # noqa: BLE001 - already returned 200 to Teams; just log
        logger.exception("Background activity processing failed")


@router.post("/messages")
async def bot_webhook(request: Request):
    try:
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > settings.max_activity_bytes:
            raise HTTPException(status_code=413, detail="Activity payload is too large.")

        # --- Bot Framework SDK path (flag-gated) --------------------------------------
        # Inbound auth + activity parsing + outbound I/O run through BotFrameworkAdapter.
        if settings.use_bot_framework_sdk:
            from botbuilder.schema import Activity
            from app.bot.adapter import get_adapter, get_bot

            body = await request.json()
            if not isinstance(body, dict) or not isinstance(body.get("type"), str):
                raise HTTPException(status_code=400, detail="Activity must be a JSON object with a type.")
            activity = Activity().deserialize(body)
            auth_header = request.headers.get("Authorization", "")
            logger.info("Incoming activity (SDK): type=%s serviceUrl=%s", activity.type, activity.service_url)
            invoke_response = await get_adapter().process_activity(activity, auth_header, get_bot().on_turn)
            if invoke_response:
                return JSONResponse(status_code=invoke_response.status, content=invoke_response.body)
            return {"status": "success"}

        # --- Raw-REST path (default, proven) ------------------------------------------
        await validate_bot_framework_authorization(request.headers.get("Authorization"))
        activity = await request.json()
        if not isinstance(activity, dict):
            raise HTTPException(status_code=400, detail="Activity must be a JSON object.")
        if not isinstance(activity.get("type"), str):
            raise HTTPException(status_code=400, detail="Activity type is required.")
        # Do not log activity bodies: Teams messages and card submissions can
        # contain personal or sensitive access-request data.
        logger.info(f"Incoming activity: type={activity.get('type')}, recipient={activity.get('recipient')}, serviceUrl={activity.get('serviceUrl')}")
        # Ack Teams immediately (its Action.Submit / message timeout is ~15s). The real work —
        # midPoint submit, card locks, confirmations — runs in the background and posts results
        # proactively. Idempotency (activity id) + the conversation lock defeat any Teams retry.
        task = asyncio.create_task(_run_activity_bg(activity))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
        return {"status": "accepted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Exception inside webhook handler: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
