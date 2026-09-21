"""Inbound midPoint lifecycle notifications → proactive Teams DM.

The JAR forwards midPoint access-request events here; this module resolves the
recipient (manager for RAISED, requester for COMPLETED) to a Teams user and sends
the appropriate Adaptive Card proactively. Kept separate from the Teams webhook
flow so the two directions do not entangle.
"""
import asyncio
import logging
from collections import OrderedDict

from app.config import settings
from app.services import directory_service, graph_service
from app.services.cards import midpoint_approval_card, midpoint_result_card
from app.services.db_service import get_db_connection
from app.services.teams_service import create_proactive_conversation, send_proactive_card, send_proactive_message

logger = logging.getLogger("teams_bot.notify")

_MOCK_ID_PREFIXES = ("29:approver-", "29:requester-")

# Bounded LRU dedupe so a recipient never gets two identical cards for the same case outcome — e.g.
# the bot-direct notification AND midPoint's own notifier (via the JAR /notify) both firing for the
# same COMPLETED event. Keyed per recipient so multi-approver RAISED still reaches every approver.
_seen_notifications: "OrderedDict[str, bool]" = OrderedDict()
_SEEN_MAX = 2000


def _dedupe_key(event: str, case_oid: str, status: str, recipient: str) -> str:
    return f"{event}|{case_oid}|{status}|{recipient.lower()}"


def _resolve_teams_target(email: str) -> tuple[str | None, str | None]:
    """Resolve (teams_user_id, service_url) for an email from the local directory + request history."""
    if not email:
        return None, None
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT teams_user_id FROM bot_user_directory WHERE lower(email) = lower(%s) AND teams_user_id IS NOT NULL LIMIT 1",
            (email,),
        )
        row = cursor.fetchone()
        teams_id = row[0] if row and row[0] else None

        service_url = None
        if teams_id:
            cursor.execute(
                "SELECT service_url FROM bot_access_requests WHERE requester_teams_id = %s AND service_url IS NOT NULL ORDER BY id DESC LIMIT 1",
                (teams_id,),
            )
            r = cursor.fetchone()
            service_url = r[0] if r and r[0] else None
        if not service_url:
            cursor.execute(
                "SELECT service_url FROM bot_access_requests WHERE lower(requester_email) = lower(%s) AND service_url IS NOT NULL ORDER BY id DESC LIMIT 1",
                (email,),
            )
            r = cursor.fetchone()
            service_url = r[0] if r and r[0] else None
    finally:
        cursor.close()
        conn.close()
    if not service_url:
        service_url = settings.teams_service_url
    return teams_id, service_url


async def _service_url_for(email: str, cached: str | None) -> str | None:
    """Prefer a cached/known serviceUrl, then request history, then the configured regional default."""
    if cached:
        return cached
    _, hist = _resolve_teams_target(email)  # reuses request-history + TEAMS_SERVICE_URL fallback
    return hist


async def _send(service_url: str, convo_id: str, card: dict, fallback_text: str, *, attempts: int = 3) -> bool:
    """Send the card proactively, retrying transient failures with a short backoff before falling
    back to a plain text message. A transient Teams 5xx/timeout must not silently lose the DM."""
    bot_id, bot_name = settings.teams_app_id, settings.bot_name
    for i in range(max(1, attempts)):
        try:
            if await send_proactive_card(service_url, convo_id, bot_id, bot_name, card):
                return True
        except Exception:  # noqa: BLE001 — treat any send error as a retryable transient
            logger.warning("Proactive card send attempt %d/%d failed for %s", i + 1, attempts, convo_id)
        if i < attempts - 1:
            await asyncio.sleep(0.5 * (i + 1))  # 0.5s, 1.0s backoff
    # Card delivery exhausted — last resort, a plain text message (also best-effort).
    try:
        return await send_proactive_message(service_url, convo_id, bot_id, bot_name, fallback_text)
    except Exception:  # noqa: BLE001
        logger.warning("Fallback text message also failed for %s", convo_id)
        return False


async def _dispatch(recipient_email: str, card: dict, fallback_text: str, display_name: str | None = None) -> bool:
    """Resolve a delivery handle for a midPoint-supplied email and send proactively.

    Order: cached proactive conversation → Graph zero-touch install → learned 29: id. The email is
    authoritative (from midPoint); mock/stale ids are never trusted.
    """
    # midPoint is the source of truth: record/refresh the identity, clearing any stale mock id.
    if settings.midpoint_authoritative:
        directory_service.upsert_midpoint(recipient_email, display_name)

    target = directory_service.get_target(recipient_email)
    service_url = await _service_url_for(recipient_email, target.get("service_url"))

    # 1) Cached proactive conversation (Graph-resolved 1:1 chat) — cheapest, no prior interaction.
    convo_ref = target.get("conversation_ref")
    if convo_ref and service_url:
        if await _send(service_url, convo_ref, card, fallback_text):
            return True
        logger.info("Cached conversation for %s failed; re-resolving.", recipient_email)

    # 2) Graph zero-touch: email → Entra user → install app → 1:1 chat id. Works cold.
    if settings.graph_proactive_enabled:
        user = await graph_service.resolve_aad_user(recipient_email)
        if user and user.get("id"):
            chat_id = await graph_service.ensure_installed_conversation(user["id"])
            if chat_id:
                su = service_url or settings.teams_service_url
                directory_service.cache_ref(recipient_email, conversation_ref=chat_id, service_url=su,
                                            aad_object_id=user["id"], source="graph")
                if su and await _send(su, chat_id, card, fallback_text):
                    return True
                logger.error("Graph-resolved chat for %s but no serviceUrl / send failed.", recipient_email)

    # 3) Legacy: a real 29: id the bot learned from a prior interaction.
    teams_id = target.get("teams_user_id")
    if teams_id and service_url:
        convo_id = await create_proactive_conversation(service_url, settings.teams_app_id, settings.bot_name,
                                                       teams_id, settings.teams_app_tenant_id)
        if convo_id and await _send(service_url, convo_id, card, fallback_text):
            return True

    logger.warning(
        "Undeliverable for %s (graph_enabled=%s, had_ref=%s, had_learned_id=%s, service_url=%s).",
        recipient_email, settings.graph_proactive_enabled, bool(convo_ref), bool(teams_id), bool(service_url),
    )
    return False


async def handle_notification(payload: dict) -> dict:
    """Route a normalized midPoint notification to the correct recipient. Returns a delivery receipt."""
    event = str(payload.get("event") or "").upper()
    recipient = str(payload.get("recipientEmail") or "").strip()
    if not recipient:
        return {"status": "no_recipient", "event": event}

    if event == "ACCESS_REQUEST_RAISED":
        card = midpoint_approval_card(payload)
        text = str(payload.get("message") or "You have a new access request awaiting approval.")
        display_name = None  # recipient is the manager; midPoint payload carries no manager name
    elif event == "ACCESS_REQUEST_COMPLETED":
        card = midpoint_result_card(payload)
        text = str(payload.get("message") or "Your access request has been updated.")
        display_name = str(payload.get("requesterName") or "") or None
    else:
        return {"status": "ignored", "event": event}

    # Dedupe: the same (event, case, outcome, recipient) may arrive twice — bot-direct and the
    # midPoint notifier via the JAR. Suppress the second so the recipient sees exactly one card.
    key = _dedupe_key(event, str(payload.get("caseOid") or ""), str(payload.get("status") or ""), recipient)
    if key in _seen_notifications:
        logger.info("Duplicate notification suppressed: %s", key)
        return {"status": "duplicate", "event": event, "recipient": recipient}
    _seen_notifications[key] = True
    while len(_seen_notifications) > _SEEN_MAX:
        _seen_notifications.popitem(last=False)

    delivered = await _dispatch(recipient, card, text, display_name)
    if not delivered:
        _seen_notifications.pop(key, None)  # allow a genuine retry to re-deliver
    return {"status": "delivered" if delivered else "undeliverable", "event": event, "recipient": recipient}
