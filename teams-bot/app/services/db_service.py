# pyrefly: ignore [missing-import]
import sqlite3
from pathlib import Path
from typing import Any

from app.config import settings

db_path = Path(getattr(settings, "sqlite_db_path", "data/accessmate.sqlite3"))
db_path.parent.mkdir(parents=True, exist_ok=True)


class SQLiteCursor:
    def __init__(self, cursor: sqlite3.Cursor):
        self._cursor = cursor

    def execute(self, sql: str, params: tuple[Any, ...] = ()):
        self._cursor.execute(sql.replace("%s", "?"), params)
        return self

    def executemany(self, sql: str, seq_of_params):
        self._cursor.executemany(sql.replace("%s", "?"), seq_of_params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def rowcount(self):
        return self._cursor.rowcount

    @property
    def lastrowid(self):
        return self._cursor.lastrowid

    def close(self):
        self._cursor.close()


class SQLiteConnection:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn

    def cursor(self):
        return SQLiteCursor(self._conn.cursor())

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def _open_connection():
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = None
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Concurrent writers (background activity tasks + inbound /notify) must wait for the
    # single SQLite write lock instead of failing immediately with "database is locked".
    conn.execute("PRAGMA busy_timeout = 8000")
    return conn


def get_db_connection():
    """Return a SQLite connection wrapper."""
    return SQLiteConnection(_open_connection())


def get_session_state(conversation_id: str) -> str:
    """Return 'open' or 'closed' for a conversation. Unknown conversations are 'open'."""
    if not conversation_id:
        return "open"
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT state FROM bot_conversation_sessions WHERE conversation_id = %s", (conversation_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row[0] if row else "open"


def set_session_state(conversation_id: str, state: str) -> None:
    """Upsert a conversation's session state ('open' | 'closed')."""
    if not conversation_id:
        return
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO bot_conversation_sessions (conversation_id, state, updated_at) VALUES (%s, %s, CURRENT_TIMESTAMP) "
        "ON CONFLICT(conversation_id) DO UPDATE SET state = excluded.state, updated_at = CURRENT_TIMESTAMP",
        (conversation_id, state),
    )
    conn.commit()
    cursor.close()
    conn.close()


def _ensure_column(cursor: SQLiteCursor, table: str, column: str, ddl: str) -> None:
    cursor.execute(f"PRAGMA table_info({table})")
    columns = {row[1] for row in cursor.fetchall()}
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db():
    """Initializes the SQLite database schema and seeds the demo directory."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS employee_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'unread'
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_access_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                requester TEXT NOT NULL,
                resource TEXT NOT NULL,
                approver TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                mcp_payload TEXT DEFAULT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                requested_role TEXT DEFAULT 'Standard Access',
                requested_target_type TEXT DEFAULT 'resource',
                requested_target_name TEXT DEFAULT NULL,
                requester_email TEXT DEFAULT NULL,
                requester_conversation_id TEXT DEFAULT NULL,
                service_url TEXT DEFAULT NULL,
                duration_days TEXT DEFAULT 'permanent',
                approver_comment TEXT DEFAULT NULL,
                requester_teams_id TEXT DEFAULT NULL,
                feedback_rating INTEGER DEFAULT NULL,
                access_until DATE DEFAULT NULL,
                risk_level TEXT DEFAULT 'medium',
                approval_stage TEXT DEFAULT 'manager',
                delegated_to TEXT DEFAULT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_user_directory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                email TEXT NOT NULL,
                teams_user_id TEXT NOT NULL,
                manager_username TEXT DEFAULT NULL
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_request_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id INTEGER NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT DEFAULT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_request_audit ON bot_request_audit_events (request_id, timestamp)")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_conversation_sessions (
                conversation_id TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'open',
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        # Idempotency / retry-dedup ledger: Teams activity ids (kind='activity') and
        # one-time card tokens (kind='card'). INSERT OR IGNORE → rowcount tells first-vs-dup.
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS bot_idempotency (
                idem_key TEXT PRIMARY KEY,
                conversation_id TEXT,
                kind TEXT NOT NULL DEFAULT 'activity',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_idem_created ON bot_idempotency (created_at)")

        # Full conversation-session lifecycle columns (see session_manager). Added idempotently
        # so existing rows/DBs upgrade in place; the legacy `state` column is reused with new values.
        for column, ddl in [
            ("session_id", "TEXT DEFAULT NULL"),
            ("user_id", "TEXT DEFAULT NULL"),
            ("tenant_id", "TEXT DEFAULT NULL"),
            ("created_at", "DATETIME DEFAULT NULL"),
            ("last_activity_at", "DATETIME DEFAULT NULL"),
            ("locked", "INTEGER DEFAULT 0"),
            ("processing", "INTEGER DEFAULT 0"),
            ("selected_action", "TEXT DEFAULT NULL"),
            ("correlation_id", "TEXT DEFAULT NULL"),
            ("expires_at", "DATETIME DEFAULT NULL"),
            ("locked_at", "DATETIME DEFAULT NULL"),
        ]:
            _ensure_column(cursor, "bot_conversation_sessions", column, ddl)

        _ensure_column(cursor, "bot_user_directory", "manager_username", "TEXT DEFAULT NULL")
        # Production proactive-delivery cache + provenance. `conversation_ref` is a Graph-resolved
        # 1:1 chat id (proactive), `aad_object_id` the Entra id, `directory_source` who last wrote
        # this row ('midpoint' | 'teams' | 'graph' | 'seed'), `updated_at` for freshness.
        for _c, _d in [
            ("conversation_ref", "TEXT DEFAULT NULL"),
            ("service_url", "TEXT DEFAULT NULL"),
            ("aad_object_id", "TEXT DEFAULT NULL"),
            ("directory_source", "TEXT DEFAULT NULL"),
            ("updated_at", "DATETIME DEFAULT NULL"),
        ]:
            _ensure_column(cursor, "bot_user_directory", _c, _d)
        for column, ddl in [
            ("requested_role", "TEXT DEFAULT 'Standard Access'"),
            ("requested_target_type", "TEXT DEFAULT 'resource'"),
            ("requested_target_name", "TEXT DEFAULT NULL"),
            ("requester_email", "TEXT DEFAULT NULL"),
            ("requester_conversation_id", "TEXT DEFAULT NULL"),
            ("service_url", "TEXT DEFAULT NULL"),
            ("duration_days", "TEXT DEFAULT 'permanent'"),
            ("approver_comment", "TEXT DEFAULT NULL"),
            ("requester_teams_id", "TEXT DEFAULT NULL"),
            ("feedback_rating", "INTEGER DEFAULT NULL"),
            ("access_until", "DATE DEFAULT NULL"),
            ("risk_level", "TEXT DEFAULT 'medium'"),
            ("approval_stage", "TEXT DEFAULT 'manager'"),
            ("delegated_to", "TEXT DEFAULT NULL"),
        ]:
            _ensure_column(cursor, "bot_access_requests", column, ddl)

        # Seed ONLY real, known Teams ids, and ONLY when the row is absent — never overwrite an id
        # the bot has learned from a real conversation (otherwise a restart clobbers it back to a
        # stale value and proactive DMs break). Fake placeholder ids (29:requester-*/29:approver-*)
        # are intentionally NOT seeded: Teams rejects them with "Failed to decrypt pairwise id", so
        # a manager/approver must message the bot once to register their real id.
        seed_rows = [
            ("IAM Team", "iam@cnxy.in", "29:1t4PEV6ZMrDOatwXFRN7ZKLjce8d63fgBSghyvzvtDZ5Z6mEq7nwjfdqGZ3L5uGf3XMG8tnYVGqFedKhSXxnb-A", "Kumar Gourab"),
            ("Kumar Gourab", "kumargourab@cnxy.in", "29:1uxCgqndeK8K18hfxyJHgfPB8zWnyKVOLZsWVpfBnH-_fK39VkK_HYxv9pktxjpHqjwXSynhHQ5aEWT352ZBBoQ", None),
        ]
        for username, email, teams_user_id, manager_username in seed_rows:
            cursor.execute(
                "INSERT OR IGNORE INTO bot_user_directory (username, email, teams_user_id, manager_username) VALUES (?, ?, ?, ?)",
                (username, email, teams_user_id, manager_username),
            )
        # Purge any previously-seeded fake ids so delivery stops failing on them.
        cursor.execute(
            "DELETE FROM bot_user_directory WHERE teams_user_id LIKE '29:requester-%' OR teams_user_id LIKE '29:approver-%'"
        )

        conn.commit()
        cursor.close()
        conn.close()
        print("Database tables initialized successfully in SQLite.")
    except Exception as e:
        print(f"[ERROR] Failed to initialize SQLite tables: {e}")
        raise
