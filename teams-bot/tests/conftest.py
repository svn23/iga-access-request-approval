"""Pytest fixtures: isolate every test on a fresh temp SQLite before app modules import."""
import os
import pathlib
import tempfile

# Point the app at a throwaway DB *before* app.config / db_service are imported anywhere.
_TMP_DB = pathlib.Path(tempfile.mkdtemp(prefix="accessmate-test-")) / "test.sqlite3"
os.environ["SQLITE_DB_PATH"] = str(_TMP_DB)

import pytest

from app.services.db_service import get_db_connection, init_db
from app.services import metrics_service


@pytest.fixture(autouse=True)
def fresh_state():
    init_db()
    # Clear the tables the session layer touches so each test starts clean.
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM bot_conversation_sessions")
    cur.execute("DELETE FROM bot_idempotency")
    conn.commit()
    cur.close()
    conn.close()
    metrics_service.reset()
    yield


def make_activity(conversation_id="c1", user_id="u1", activity_id=None, text="", action=None, card_token=None, tenant="t1"):
    import uuid
    value = None
    if action:
        value = {"action": action}
        if card_token:
            value["card_token"] = card_token
    return {
        "type": "message",
        "id": activity_id or uuid.uuid4().hex,
        "text": text,
        "value": value,
        "from": {"id": user_id, "name": "Tester"},
        "conversation": {"id": conversation_id, "tenantId": tenant},
        "serviceUrl": "https://smba.example/",
        "recipient": {"id": "bot", "name": "AccessMate"},
    }
