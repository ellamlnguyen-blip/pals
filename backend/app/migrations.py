from __future__ import annotations

import sqlite3


MIGRATIONS = [
    """CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, category TEXT NOT NULL,
        event_when TEXT NOT NULL, location TEXT NOT NULL, description TEXT NOT NULL,
        people TEXT NOT NULL DEFAULT '[]', lat REAL NOT NULL, lng REAL NOT NULL,
        starts_at TEXT, ends_at TEXT, created_by TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS event_rsvps (
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        password_hash TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS conversations (
        id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        sender_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deleted_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS event_admins (
        event_id TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS friendships (
        requester_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        addressee_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (requester_id, addressee_id)
    )""",
    """CREATE TABLE IF NOT EXISTS notifications (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, body TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
        read_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""",
    """CREATE TABLE IF NOT EXISTS direct_messages (
        id TEXT PRIMARY KEY, sender_id TEXT NOT NULL REFERENCES users(id),
        recipient_id TEXT NOT NULL REFERENCES users(id), body TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, read_at TEXT
    )""",
]


def migrate(connection: object) -> None:
    connection.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)")
    applied = {row["version"] if isinstance(row, dict) else row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    for version, statement in enumerate(MIGRATIONS, start=1):
        if version in applied:
            continue
        connection.execute(statement)
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
    # Preserve databases created before migrations were introduced.
    columns = ({row[1] for row in connection.execute("PRAGMA table_info(events)")} if not getattr(connection, "postgres", False) else {row["column_name"] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'events'")})
    for name, definition in (("starts_at", "TEXT"), ("ends_at", "TEXT"), ("created_by", "TEXT"), ("chat_icon", "TEXT")):
        if name not in columns:
            connection.execute(f"ALTER TABLE events ADD COLUMN {name} {definition}")
    user_columns = ({row[1] for row in connection.execute("PRAGMA table_info(users)")} if not getattr(connection, "postgres", False) else {row["column_name"] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'users'")})
    for name, definition in (("photo", "TEXT"), ("photo_gallery", "TEXT"), ("photo_position_x", "REAL NOT NULL DEFAULT 50"), ("photo_position_y", "REAL NOT NULL DEFAULT 50"), ("photo_zoom", "REAL NOT NULL DEFAULT 1"), ("instagram", "TEXT"), ("school", "TEXT"), ("year", "TEXT"), ("major", "TEXT"), ("hometown", "TEXT"), ("bio", "TEXT"), ("hobbies", "TEXT"), ("goals", "TEXT"), ("friend_activities", "TEXT"), ("fun_facts", "TEXT"), ("things_to_do", "TEXT"), ("favorite_foods", "TEXT"), ("favorite_music", "TEXT")):
        if name not in user_columns:
            connection.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")
    session_columns = ({row[1] for row in connection.execute("PRAGMA table_info(sessions)")} if not getattr(connection, "postgres", False) else {row["column_name"] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'sessions'")})
    if "expires_at" not in session_columns:
        connection.execute("ALTER TABLE sessions ADD COLUMN expires_at TEXT")
    for table in ("messages", "direct_messages"):
        columns = ({row[1] for row in connection.execute(f"PRAGMA table_info({table})")} if not getattr(connection, "postgres", False) else {row["column_name"] for row in connection.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))})
        for name, definition in (("kind", "TEXT NOT NULL DEFAULT 'text'"), ("attachment_url", "TEXT")):
            if name not in columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
