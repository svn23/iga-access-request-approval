"""Delivery-target directory: maps a midPoint email to a Teams delivery handle.

midPoint is the source of truth for identity (email, name, manager relationship). This module
only caches the *platform delivery handle* keyed by email — a Graph-resolved 1:1 chat id
(`conversation_ref`) or a learned `29:` user id. When midPoint sends fresh data it overwrites the
local name/provenance and clears stale mock ids; genuinely-learned delivery ids are preserved.
"""
import logging
from datetime import datetime, timezone

from app.services.db_service import get_db_connection

logger = logging.getLogger("teams_bot.directory")

_MOCK_PREFIXES = ("29:approver-", "29:requester-")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_real_teams_id(v: str | None) -> bool:
    return bool(v) and v.startswith("29:") and not v.startswith(_MOCK_PREFIXES)


def get_target(email: str) -> dict:
    """Best delivery handle for an email: {teams_user_id, conversation_ref, service_url, aad_object_id}.

    Prefers a row that already has a proactive conversation_ref, then the most recently updated.
    """
    if not email:
        return {}
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT teams_user_id, conversation_ref, service_url, aad_object_id "
            "FROM bot_user_directory WHERE lower(email) = lower(%s) "
            "ORDER BY (conversation_ref IS NOT NULL) DESC, updated_at DESC LIMIT 1",
            (email,),
        )
        row = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    if not row:
        return {}
    tid = row[0]
    return {
        "teams_user_id": tid if _is_real_teams_id(tid) else None,
        "conversation_ref": row[1],
        "service_url": row[2],
        "aad_object_id": row[3],
    }


def upsert_midpoint(email: str, display_name: str | None = None) -> None:
    """midPoint-authoritative upsert: ensure a row for this email, stamp provenance, and clear any
    stale mock id so it is re-resolved. Never clobbers a real teams id or conversation_ref."""
    if not email:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id, teams_user_id FROM bot_user_directory WHERE lower(email) = lower(%s) LIMIT 1", (email,))
        row = cur.fetchone()
        if row:
            rid, tid = row[0], row[1]
            if tid and tid.startswith(_MOCK_PREFIXES):
                cur.execute("UPDATE bot_user_directory SET teams_user_id='', directory_source='midpoint', updated_at=%s WHERE id=%s", (_now(), rid))
            else:
                cur.execute("UPDATE bot_user_directory SET directory_source='midpoint', updated_at=%s WHERE id=%s", (_now(), rid))
        else:
            uname = (display_name or email)[:255]
            try:
                cur.execute(
                    "INSERT INTO bot_user_directory (username, email, teams_user_id, directory_source, updated_at) VALUES (%s, %s, '', 'midpoint', %s)",
                    (uname, email, _now()),
                )
            except Exception:  # username collision → fall back to email as username
                cur.execute(
                    "INSERT OR IGNORE INTO bot_user_directory (username, email, teams_user_id, directory_source, updated_at) VALUES (%s, %s, '', 'midpoint', %s)",
                    (email, email, _now()),
                )
        conn.commit()
    except Exception:
        logger.exception("directory upsert_midpoint failed for %s", email)
    finally:
        cur.close()
        conn.close()


def cache_ref(email: str, *, conversation_ref: str | None = None, service_url: str | None = None,
              aad_object_id: str | None = None, teams_user_id: str | None = None, source: str = "graph") -> None:
    """Persist a resolved delivery handle so later notifications skip re-resolution."""
    if not email:
        return
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT id FROM bot_user_directory WHERE lower(email) = lower(%s) LIMIT 1", (email,))
        row = cur.fetchone()
        sets, vals = [], []
        if conversation_ref is not None:
            sets.append("conversation_ref=%s"); vals.append(conversation_ref)
        if service_url is not None:
            sets.append("service_url=%s"); vals.append(service_url)
        if aad_object_id is not None:
            sets.append("aad_object_id=%s"); vals.append(aad_object_id)
        if teams_user_id:
            sets.append("teams_user_id=%s"); vals.append(teams_user_id)
        sets.append("directory_source=%s"); vals.append(source)
        sets.append("updated_at=%s"); vals.append(_now())
        if row:
            cur.execute(f"UPDATE bot_user_directory SET {', '.join(sets)} WHERE id=%s", (*vals, row[0]))
        else:
            cur.execute(
                "INSERT OR IGNORE INTO bot_user_directory (username, email, teams_user_id, conversation_ref, service_url, aad_object_id, directory_source, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (email, email, teams_user_id or "", conversation_ref, service_url, aad_object_id, source, _now()),
            )
        conn.commit()
    except Exception:
        logger.exception("directory cache_ref failed for %s", email)
    finally:
        cur.close()
        conn.close()
