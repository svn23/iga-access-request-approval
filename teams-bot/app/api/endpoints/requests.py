import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from app.services.bot_auth import validate_bot_framework_authorization
from app.services.db_service import get_db_connection

logger = logging.getLogger("teams_bot.api.requests")
router = APIRouter()


class NotificationRequest(BaseModel):
    transactionId: str = Field(min_length=1, max_length=80)
    recipientEmail: EmailStr
    audience: str = Field(min_length=1, max_length=20)
    subject: str = Field(min_length=1, max_length=180)
    message: str = Field(min_length=1, max_length=4000)


@router.get("/requests/{email}/status")
async def request_status(request: Request, email: str):
    try:
        await validate_bot_framework_authorization(request.headers.get("Authorization"))
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, resource, requested_role, requested_target_type, requested_target_name, status, approver, access_until, timestamp
            FROM bot_access_requests
            WHERE requester_email = %s
            ORDER BY timestamp DESC
            LIMIT 10
            """,
            (email,)
        )
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return {
            "status": "accepted",
            "email": email,
            "requests": [
                {
                    "requestId": row[0],
                    "resource": row[1],
                    "role": row[2],
                    "targetType": row[3],
                    "targetName": row[4],
                    "requestStatus": row[5],
                    "approver": row[6],
                    "accessUntil": row[7].isoformat() if row[7] else None,
                    "timestamp": row[8].isoformat() if row[8] else None,
                }
                for row in rows
            ],
        }
    except Exception as exc:
        logger.error("Status lookup failed for %s: %s", email, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load request status.")


@router.post("/notifications/send")
async def send_notification(request: Request, payload: NotificationRequest):
    try:
        await validate_bot_framework_authorization(request.headers.get("Authorization"))
        return {
            "status": "accepted",
            "transactionId": payload.transactionId,
            "recipientEmail": str(payload.recipientEmail),
            "audience": payload.audience,
            "subject": payload.subject,
            "message": payload.message,
            "delivery": "prepared",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Notification prepare failed for %s: %s", payload.recipientEmail, exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not prepare notification.")
