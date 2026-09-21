"""Conversation session lifecycle, locking, idempotency and interaction security.

Single-instance design over SQLite: atomicity comes from SQLite's per-write serialization plus
compare-and-swap `UPDATE ... WHERE <expected state>` (rowcount tells the winner). No external lock
service. For multi-instance/HA swap the CAS + idempotency ledger for Redis/Cosmos (see DEV.md).

State machine:
    NEW -> ACTIVE -> WAITING_FOR_BUTTON -> PROCESSING -> RESPONDING -> COMPLETED
                         ^                     |                          |
                         |  (exception:        |                          v
                         +--- unlock_keep_alive)                       EXPIRED (idle timeout, any non-terminal)
"""
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.config import settings
from app.services import metrics_service
from app.services.db_service import get_db_connection

logger = logging.getLogger("teams_bot.session")

# States
NEW = "NEW"
ACTIVE = "ACTIVE"
WAITING_FOR_BUTTON = "WAITING_FOR_BUTTON"
PROCESSING = "PROCESSING"
RESPONDING = "RESPONDING"
COMPLETED = "COMPLETED"
EXPIRED = "EXPIRED"

_TERMINAL = {COMPLETED, EXPIRED}
_LOCKABLE = {ACTIVE, WAITING_FOR_BUTTON}
# Legacy values written by the old open/closed helpers map onto the new machine.
_LEGACY = {"open": ACTIVE, "closed": COMPLETED}

# Standard user-facing replies (kept identical to the requirement wording).
MSG_PROCESSING_WAIT = "I'm still processing your previous request. Please wait."
MSG_ALREADY_PROCESSED = "This request has already been processed."
MSG_ENDED = "This conversation has ended. Please type 'Hi' to start a new request."
MSG_FINISHED = "This session has already finished. Type 'Hi' to begin a new session."


def _now() -> datetime:
    return datetime.utcnow()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def greeting_keywords() -> set[str]:
    return {k.strip().lower() for k in (settings.greeting_keywords or "").split(",") if k.strip()}


def _greeting_regex() -> re.Pattern:
    # Each keyword matches with its final letter repeatable ("hi" -> hi/hii/hiii, "hello" -> hello/helloo),
    # trailing punctuation/space ignored — matching the bot's historical greeting behaviour.
    kws = greeting_keywords() or {"hi", "hello", "hey", "start"}
    alts = "|".join(re.escape(k[:-1]) + re.escape(k[-1]) + "+" for k in kws if k)
    return re.compile(rf"^(?:{alts})[\W]*$", re.IGNORECASE)


def is_greeting(text: str) -> bool:
    return bool(text) and bool(_greeting_regex().fullmatch(text.strip()))


@dataclass
class Decision:
    allow: bool
    reason: str
    reply: str | None = None
    session: dict | None = None
    correlation_id: str | None = None


# ---------------------------------------------------------------- reads

def get_session(conversation_id: str) -> dict | None:
    if not conversation_id:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "SELECT conversation_id, session_id, user_id, tenant_id, state, locked, processing, "
            "created_at, last_activity_at, expires_at, locked_at, correlation_id "
            "FROM bot_conversation_sessions WHERE conversation_id = %s",
            (conversation_id,),
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        conn.close()
    if not row:
        return None
    cols = ["conversation_id", "session_id", "user_id", "tenant_id", "state", "locked", "processing",
            "created_at", "last_activity_at", "expires_at", "locked_at", "correlation_id"]
    s = dict(zip(cols, row))
    s["state"] = _LEGACY.get(s.get("state"), s.get("state") or NEW)
    return s


# ---------------------------------------------------------------- idempotency

def _insert_idem(idem_key: str, conversation_id: str, kind: str) -> bool:
    """True if this key is new (first time); False if it was already recorded (duplicate)."""
    if not idem_key:
        return True
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT OR IGNORE INTO bot_idempotency (idem_key, conversation_id, kind) VALUES (%s, %s, %s)",
            (f"{kind}:{idem_key}", conversation_id, kind),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        cursor.close()
        conn.close()


def seen_activity(activity_id: str, conversation_id: str) -> bool:
    """Record + report first-sight of a Teams activity id (defeats Teams/network retries)."""
    return _insert_idem(activity_id, conversation_id, "activity")


def consume_token(card_token: str, conversation_id: str) -> bool:
    """Atomically consume a one-time card token. True on first use, False if already used."""
    return _insert_idem(card_token, conversation_id, "card")


# ---------------------------------------------------------------- lifecycle

def start_session(activity: dict) -> dict:
    """Begin a fresh session for a conversation (greeting). Overwrites any prior session row."""
    conversation_id = str(activity.get("conversation", {}).get("id") or "")
    user_id = str(activity.get("from", {}).get("id") or "")
    tenant_id = str(
        activity.get("channelData", {}).get("tenant", {}).get("id")
        or activity.get("conversation", {}).get("tenantId")
        or ""
    )
    now = _now()
    expires = now + timedelta(minutes=settings.session_timeout_minutes)
    session_id = str(uuid.uuid4())
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "INSERT INTO bot_conversation_sessions "
            "(conversation_id, session_id, user_id, tenant_id, state, locked, processing, "
            " created_at, last_activity_at, expires_at, locked_at, correlation_id, selected_action, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, 0, 0, %s, %s, %s, NULL, NULL, NULL, %s) "
            "ON CONFLICT(conversation_id) DO UPDATE SET session_id=excluded.session_id, user_id=excluded.user_id, "
            " tenant_id=excluded.tenant_id, state=excluded.state, locked=0, processing=0, created_at=excluded.created_at, "
            " last_activity_at=excluded.last_activity_at, expires_at=excluded.expires_at, locked_at=NULL, "
            " correlation_id=NULL, selected_action=NULL, updated_at=excluded.updated_at",
            (conversation_id, session_id, user_id, tenant_id, ACTIVE, _iso(now), _iso(now), _iso(expires), _iso(now)),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    metrics_service.increment(metrics_service.SESSIONS_STARTED)
    logger.info("session started", extra={"session_id": session_id, "conversation_id": conversation_id})
    return get_session(conversation_id)


def touch(conversation_id: str) -> None:
    """Bump last activity + slide the idle-expiry window forward."""
    now = _now()
    expires = now + timedelta(minutes=settings.session_timeout_minutes)
    _update(conversation_id, "SET last_activity_at=%s, expires_at=%s, updated_at=%s",
            (_iso(now), _iso(expires), _iso(now)))


def acquire_lock(conversation_id: str, correlation_id: str) -> bool:
    """Compare-and-swap into PROCESSING. Wins only from a lockable state with no live lock
    (a lock older than processing_timeout_seconds is reclaimable — crash recovery)."""
    now = _now()
    stale_cutoff = _iso(now - timedelta(seconds=settings.processing_timeout_seconds))
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE bot_conversation_sessions "
            "SET state=%s, locked=1, processing=1, locked_at=%s, correlation_id=%s, last_activity_at=%s, updated_at=%s "
            "WHERE conversation_id=%s AND state IN (%s, %s) "
            "  AND (locked=0 OR locked IS NULL OR locked_at < %s)",
            (PROCESSING, _iso(now), correlation_id, _iso(now), _iso(now),
             conversation_id, ACTIVE, WAITING_FOR_BUTTON, stale_cutoff),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        cursor.close()
        conn.close()


def to_state(conversation_id: str, new_state: str, expected: set[str]) -> bool:
    """Guarded transition: only fires from an expected current state (rejects illegal transitions)."""
    placeholders = ", ".join(["%s"] * len(expected))
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            f"UPDATE bot_conversation_sessions SET state=%s, last_activity_at=%s, updated_at=%s "
            f"WHERE conversation_id=%s AND state IN ({placeholders})",
            (new_state, _iso(_now()), _iso(_now()), conversation_id, *expected),
        )
        conn.commit()
        ok = cursor.rowcount == 1
    finally:
        cursor.close()
        conn.close()
    if not ok:
        logger.warning("illegal/again transition -> %s rejected", new_state,
                       extra={"conversation_id": conversation_id})
    return ok


def complete(conversation_id: str) -> None:
    _update(conversation_id, "SET state=%s, locked=0, processing=0, locked_at=NULL, last_activity_at=%s, updated_at=%s",
            (COMPLETED, _iso(_now()), _iso(_now())))
    metrics_service.increment(metrics_service.SESSIONS_COMPLETED)
    logger.info("session completed", extra={"conversation_id": conversation_id})


def unlock_keep_alive(conversation_id: str) -> None:
    """Exception recovery: drop the lock but keep the session usable for a retry."""
    _update(conversation_id, "SET state=%s, locked=0, processing=0, locked_at=NULL, last_activity_at=%s, updated_at=%s",
            (WAITING_FOR_BUTTON, _iso(_now()), _iso(_now())))
    logger.info("session unlocked (kept alive after failure)", extra={"conversation_id": conversation_id})


def expire_due() -> int:
    """Sweep idle sessions to EXPIRED. Returns how many were expired."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "UPDATE bot_conversation_sessions SET state=%s, locked=0, processing=0, updated_at=%s "
            "WHERE expires_at IS NOT NULL AND expires_at < %s AND state NOT IN (%s, %s)",
            (EXPIRED, _iso(_now()), _iso(_now()), COMPLETED, EXPIRED),
        )
        conn.commit()
        n = cursor.rowcount
    finally:
        cursor.close()
        conn.close()
    if n > 0:
        metrics_service.increment(metrics_service.SESSIONS_EXPIRED, n)
    return n


def _update(conversation_id: str, set_clause: str, params: tuple) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"UPDATE bot_conversation_sessions {set_clause} WHERE conversation_id=%s",
                       (*params, conversation_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------- the gate

def begin_activity(activity: dict, *, is_greeting: bool, is_button: bool, action: str | None,
                   card_token: str | None, governs_session: bool) -> Decision:
    """Single entry gate. Enforces retry-dedup, one-time buttons, session state, and security.

    `governs_session` is False for approver/manager actions (a different conversation with no
    requester session) — those get idempotency + one-time buttons only, never session gating.
    """
    conversation_id = str(activity.get("conversation", {}).get("id") or "")
    user_id = str(activity.get("from", {}).get("id") or "")
    activity_id = str(activity.get("id") or "")
    correlation_id = str(uuid.uuid4())

    # Security: reject structurally invalid activities (spoofing/replay hardening).
    if not conversation_id or not user_id or not activity_id:
        metrics_service.increment(metrics_service.BLOCKED_MESSAGES)
        return Decision(False, "invalid_activity", None, None, correlation_id)

    # 1. Teams/network retry dedup (same activity id) — silently ignore replays.
    if not seen_activity(activity_id, conversation_id):
        metrics_service.increment(metrics_service.RETRIES_IGNORED)
        logger.info("duplicate activity ignored", extra={"conversation_id": conversation_id, "activity_id": activity_id})
        return Decision(False, "retry", None, None, correlation_id)

    # 2. One-time card token (defeats double/rapid clicks and stale-card replays).
    if is_button and card_token and not consume_token(card_token, conversation_id):
        metrics_service.increment(metrics_service.DUPLICATE_CLICKS)
        logger.info("duplicate click ignored", extra={"conversation_id": conversation_id, "action": action})
        return Decision(False, "consumed", MSG_ALREADY_PROCESSED, None, correlation_id)

    # Lazy idle-expiry sweep for this conversation before we evaluate state.
    expire_due()
    session = get_session(conversation_id)

    # Approver/manager actions are session-agnostic: idempotency already applied, allow through.
    if not governs_session:
        return Decision(True, "ok", None, session, correlation_id)

    # 3. Greeting starts/restarts a session — but not while a request is actively processing.
    if is_greeting:
        if session and (session["state"] == PROCESSING or session.get("locked")):
            metrics_service.increment(metrics_service.BLOCKED_MESSAGES)
            return Decision(False, "locked", MSG_PROCESSING_WAIT, session, correlation_id)
        return Decision(True, "greeting", None, session, correlation_id)

    # 4. No session, or a finished/expired one → only a greeting may proceed.
    if not session or session["state"] in _TERMINAL:
        metrics_service.increment(metrics_service.BLOCKED_MESSAGES)
        return Decision(False, "ended", MSG_ENDED, session, correlation_id)

    # 5. Busy: a request is being processed — block every new input.
    if session["state"] == PROCESSING or session.get("locked"):
        metrics_service.increment(metrics_service.BLOCKED_MESSAGES)
        return Decision(False, "locked", MSG_PROCESSING_WAIT, session, correlation_id)

    # 6. Session hijack guard: the actor must own the session.
    if session.get("user_id") and user_id != session["user_id"]:
        metrics_service.increment(metrics_service.BLOCKED_MESSAGES)
        logger.warning("session owner mismatch", extra={"conversation_id": conversation_id})
        return Decision(False, "hijack", MSG_ENDED, session, correlation_id)

    touch(conversation_id)
    return Decision(True, "ok", None, session, correlation_id)
