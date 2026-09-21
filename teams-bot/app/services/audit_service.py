"""Append-only, parameterized request audit events."""
import json
import logging
from app.services.db_service import get_db_connection

logger = logging.getLogger("teams_bot.audit")

def record_audit_event(request_id: int | None, actor: str, event_type: str, details: dict | None = None) -> None:
    # Requests are no longer stored in the bot DB (midPoint is the source of truth), so a
    # submit-time audit has no request_id. The table's request_id is NOT NULL, so skip cleanly
    # rather than raising; the correlation ref lives in `details` and the structured logs.
    if request_id is None:
        logger.info("audit %s by %s: %s", event_type, actor, json.dumps(details or {}))
        return
    conn = None
    try:
        conn = get_db_connection(); cursor = conn.cursor()
        cursor.execute("INSERT INTO bot_request_audit_events (request_id, actor, event_type, details) VALUES (%s, %s, %s, %s)",
                       (request_id, actor[:255], event_type[:80], json.dumps(details or {})))
        conn.commit(); cursor.close()
    except Exception:
        logger.exception("Could not record audit event for request %s", request_id)
    finally:
        # Always close — a failed INSERT that leaves the connection open holds the SQLite write
        # lock and cascades into "database is locked" for every later writer.
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
