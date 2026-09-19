from __future__ import annotations

import json
import os
import secrets
import sqlite3
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from .migrations import migrate

DEFAULT_DATABASE_PATH = Path(__file__).resolve().parent.parent / "pals.db"


class DatabaseConnection:
    def __init__(self, connection: Any, postgres: bool = False) -> None:
        self.connection = connection
        self.postgres = postgres

    def _query(self, query: str) -> str:
        return query.replace("?", "%s") if self.postgres else query

    def execute(self, query: str, params: Any = ()) -> Any:
        return self.connection.execute(self._query(query), params)

    def executemany(self, query: str, params: Any) -> Any:
        return self.connection.executemany(self._query(query), params)

    def __enter__(self) -> "DatabaseConnection":
        self.connection.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self.connection.__exit__(*args)


class EventStore:
    def __init__(self, database_path: str | Path | None = None) -> None:
        configured_path = database_path or os.getenv("PALS_DATABASE_PATH")
        self.database_path = Path(configured_path) if configured_path else DEFAULT_DATABASE_PATH
        self.database_url = os.getenv("DATABASE_URL")

    def _connect(self) -> DatabaseConnection:
        if self.database_url and self.database_url.startswith(("postgres://", "postgresql://")):
            import psycopg
            from psycopg.rows import dict_row
            return DatabaseConnection(psycopg.connect(self.database_url, row_factory=dict_row), postgres=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return DatabaseConnection(connection)

    def initialize(self, seed_events: list[dict[str, Any]] | None = None) -> None:
        with self._connect() as connection:
            migrate(connection)
            self._remove_synthetic_smoke_accounts(connection)
            event_count = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
            if seed_events and event_count == 0:
                connection.executemany(
                    """INSERT INTO events
                    (id, title, category, event_when, location, description, people, lat, lng, starts_at, ends_at, chat_icon)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [(e["id"], e["title"], e["category"], e["when"], e["where"],
                      e["desc"], json.dumps(e.get("people", [])), e["lat"], e["lng"],
                    e.get("starts_at"), e.get("ends_at"), e.get("chat_icon"))
                     for e in seed_events],
                )
            if connection.postgres:
                connection.execute("""UPDATE events SET starts_at = COALESCE(starts_at, CURRENT_TIMESTAMP::text),
                    ends_at = COALESCE(ends_at, (CURRENT_TIMESTAMP + INTERVAL '2 hours')::text) WHERE starts_at IS NULL""")
            else:
                connection.execute("""UPDATE events SET starts_at = COALESCE(starts_at, datetime('now')),
                    ends_at = COALESCE(ends_at, datetime('now', '+2 hours')) WHERE starts_at IS NULL""")

    @staticmethod
    def _remove_synthetic_smoke_accounts(connection: DatabaseConnection) -> None:
        """Keep deployment smoke-test accounts out of the real People directory."""
        rows = connection.execute(
            "SELECT id FROM users WHERE email LIKE ?",
            ("release-smoke-%@example.com",),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return
        for user_id in ids:
            for table, column in (("sessions", "user_id"), ("event_rsvps", "user_id"), ("event_admins", "user_id"), ("friendships", "requester_id"), ("friendships", "addressee_id"), ("notifications", "user_id"), ("direct_messages", "sender_id"), ("direct_messages", "recipient_id")):
                connection.execute(f"DELETE FROM {table} WHERE {column} = ?", (user_id,))
            connection.execute("DELETE FROM messages WHERE sender_id = ?", (user_id,))
            connection.execute("DELETE FROM users WHERE id = ?", (user_id,))

    def list_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT * FROM events
                WHERE starts_at IS NULL OR starts_at >= ?
                ORDER BY COALESCE(starts_at, event_when), id""", ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(),)
            ).fetchall()
            attendees = connection.execute(
                "SELECT event_id, user_id FROM event_rsvps ORDER BY created_at, user_id"
            ).fetchall()
        attendee_map: dict[str, list[str]] = {}
        for attendee in attendees:
            attendee_map.setdefault(attendee["event_id"], []).append(attendee["user_id"])
        return [self._serialize(row, attendee_map.get(row["id"], [])) for row in rows]

    def rsvp(self, event_id: str, user_id: str) -> dict[str, str]:
        with self._connect() as connection:
            event = connection.execute("SELECT id, title, created_by FROM events WHERE id = ?", (event_id,)).fetchone()
            if event is None:
                raise KeyError("event not found")
            connection.execute(
                "INSERT INTO event_rsvps (event_id, user_id) VALUES (?, ?)",
                (event_id, user_id),
            )
            recipients = {event["created_by"]} if event["created_by"] and event["created_by"] != user_id else set()
            admins = connection.execute("SELECT user_id FROM event_admins WHERE event_id = ?", (event_id,)).fetchall()
            recipients.update(row["user_id"] for row in admins if row["user_id"] != user_id)
            for recipient_id in recipients:
                notification_id = secrets.token_urlsafe(12)
                connection.execute(
                    "INSERT INTO notifications (id, user_id, kind, body, data) VALUES (?, ?, 'event_attendance', ?, ?)",
                    (notification_id, recipient_id, "Someone joined your event", json.dumps({"event_id": event_id, "user_id": user_id, "title": event["title"]})),
                )
        return {"event_id": event_id, "user_id": user_id, "status": "going"}

    def create_user(self, email: str, name: str, password: str) -> dict[str, str]:
        user_id = secrets.token_urlsafe(12)
        password_hash = self._hash_password(password)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO users (id, email, name, password_hash) VALUES (?, ?, ?, ?)",
                (user_id, email.lower(), name, password_hash),
            )
        return {"id": user_id, "email": email.lower(), "name": name}

    def authenticate(self, email: str, password: str) -> dict[str, str] | None:
        with self._connect() as connection:
            user = connection.execute("SELECT * FROM users WHERE email = ?", (email.lower(),)).fetchone()
            if user is None or not self._verify_password(password, user["password_hash"]):
                return None
            token = secrets.token_urlsafe(32)
            expires_at = (datetime.now(timezone.utc) + timedelta(hours=float(os.getenv("PALS_SESSION_HOURS", "168")))).isoformat()
            connection.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?, ?, ?)", (token, user["id"], expires_at))
        return {"token": token, "user_id": user["id"], "email": user["email"], "name": user["name"]}

    def get_user_by_token(self, token: str) -> dict[str, str] | None:
        with self._connect() as connection:
            expiration_clause = "(s.expires_at IS NULL OR s.expires_at::timestamptz > CURRENT_TIMESTAMP)" if connection.postgres else "(s.expires_at IS NULL OR s.expires_at > CURRENT_TIMESTAMP)"
            user = connection.execute(
                f"SELECT u.id, u.email, u.name, u.photo, u.photo_gallery, u.photo_position_x, u.photo_position_y, u.photo_zoom, u.instagram, u.school, u.year, u.major, u.hometown, u.bio, u.hobbies, u.goals, u.friend_activities, u.fun_facts, u.things_to_do, u.favorite_foods, u.favorite_music FROM users u JOIN sessions s ON s.user_id = u.id WHERE s.token = ? AND {expiration_clause}",
                (token,),
            ).fetchone()
        return dict(user) if user else None

    def delete_session(self, token: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def list_profiles(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, name, photo, photo_gallery, photo_position_x, photo_position_y, photo_zoom, instagram, school, year, major, hometown, bio, hobbies, goals, friend_activities, fun_facts, things_to_do, favorite_foods, favorite_music FROM users ORDER BY name").fetchall()
        return [self._profile(row) for row in rows]

    def get_profile(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute("SELECT id, name, photo, photo_gallery, photo_position_x, photo_position_y, photo_zoom, instagram, school, year, major, hometown, bio, hobbies, goals, friend_activities, fun_facts, things_to_do, favorite_foods, favorite_music FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._profile(row) if row else None

    def update_profile(self, user_id: str, profile: dict[str, Any]) -> dict[str, Any] | None:
        fields = ("name", "photo", "photo_gallery", "photo_position_x", "photo_position_y", "photo_zoom", "instagram", "school", "year", "major", "hometown", "bio", "hobbies", "goals", "friend_activities", "fun_facts", "things_to_do", "favorite_foods", "favorite_music")
        values = {field: profile.get(field) for field in fields}
        values["photo_gallery"] = json.dumps((profile.get("photo_gallery") or [])[:4])
        values["hobbies"] = json.dumps(profile.get("hobbies", []))
        with self._connect() as connection:
            cursor = connection.execute("UPDATE users SET name = ?, photo = ?, photo_gallery = ?, photo_position_x = ?, photo_position_y = ?, photo_zoom = ?, instagram = ?, school = ?, year = ?, major = ?, hometown = ?, bio = ?, hobbies = ?, goals = ?, friend_activities = ?, fun_facts = ?, things_to_do = ?, favorite_foods = ?, favorite_music = ? WHERE id = ?", (*[values[field] for field in fields], user_id))
            if cursor.rowcount == 0:
                return None
        return self.get_profile(user_id)

    @staticmethod
    def _profile(row: sqlite3.Row) -> dict[str, Any]:
        profile = dict(row)
        profile["hobbies"] = json.loads(profile.get("hobbies") or "[]")
        profile["photo_gallery"] = json.loads(profile.get("photo_gallery") or "[]")
        return profile

    @staticmethod
    def _hash_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 260_000)
        return f"{salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_password(password: str, encoded: str) -> bool:
        salt_hex, digest_hex = encoded.split("$", 1)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 260_000)
        return secrets.compare_digest(digest.hex(), digest_hex)

    def cancel_rsvp(self, event_id: str, user_id: str) -> None:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone() is None:
                raise KeyError("event not found")
            connection.execute(
                "DELETE FROM event_rsvps WHERE event_id = ? AND user_id = ?",
                (event_id, user_id),
            )

    def list_rsvps(self, user_id: str) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id FROM event_rsvps WHERE user_id = ?", (user_id,)
            ).fetchall()
        return [row["event_id"] for row in rows]

    def _conversation_for_event(self, connection: sqlite3.Connection, event_id: str) -> str | None:
        event = connection.execute("SELECT id FROM events WHERE id = ?", (event_id,)).fetchone()
        if event is None:
            return None
        connection.execute("INSERT INTO conversations (id, event_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (event_id, event_id))
        return event_id

    def can_message_event(self, event_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            return connection.execute(
                """SELECT 1 FROM events e WHERE e.id = ?
                AND (e.starts_at IS NULL OR e.starts_at >= ?)
                AND (e.created_by = ? OR EXISTS (
                    SELECT 1 FROM event_rsvps r WHERE r.event_id = e.id AND r.user_id = ?
                ))""",
                (event_id, (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(), user_id, user_id),
            ).fetchone() is not None

    def list_messages(self, event_id: str, limit: int = 50, before: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as connection:
            conversation_id = self._conversation_for_event(connection, event_id)
            if conversation_id is None:
                raise KeyError("event not found")
            query = """SELECT m.id, m.body, m.kind, m.attachment_url, m.created_at, m.sender_id, u.name AS sender_name
                       FROM messages m JOIN users u ON u.id = m.sender_id
                       WHERE m.conversation_id = ? AND m.deleted_at IS NULL"""
            params: list[Any] = [conversation_id]
            if before:
                query += " AND m.created_at < ?"
                params.append(before)
            query += " ORDER BY m.created_at DESC LIMIT ?"
            params.append(min(max(limit, 1), 100))
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in reversed(rows)]

    def create_message(self, event_id: str, sender_id: str, body: str, kind: str = "text", attachment_url: str | None = None) -> dict[str, Any]:
        with self._connect() as connection:
            conversation_id = self._conversation_for_event(connection, event_id)
            if conversation_id is None:
                raise KeyError("event not found")
            message_id = secrets.token_urlsafe(12)
            connection.execute(
                "INSERT INTO messages (id, conversation_id, sender_id, body, kind, attachment_url) VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, conversation_id, sender_id, body.strip(), kind, attachment_url),
            )
            row = connection.execute(
                "SELECT m.id, m.body, m.kind, m.attachment_url, m.created_at, m.sender_id, u.name AS sender_name FROM messages m JOIN users u ON u.id = m.sender_id WHERE m.id = ?",
                (message_id,),
            ).fetchone()
        return dict(row)

    def delete_message(self, message_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE messages SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND sender_id = ? AND deleted_at IS NULL",
                (message_id, user_id),
            )
        return cursor.rowcount > 0

    def mark_notification_read(self, notification_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("UPDATE notifications SET read_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?", (notification_id, user_id))
        return cursor.rowcount > 0

    def create_event(self, event: dict[str, Any], created_by: str) -> dict[str, Any]:
        starts_at = event.get("starts_at") or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        ends_at = event.get("ends_at") or (datetime.fromisoformat(starts_at) + timedelta(hours=2)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO events
                (id, title, category, event_when, location, description, people, lat, lng, starts_at, ends_at, created_by, chat_icon)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event["id"], event["title"], event["category"], event["when"], event["where"],
                 event["desc"], json.dumps(event.get("people", [])), event["lat"], event["lng"], starts_at, ends_at, created_by, event.get("chat_icon")),
            )
            connection.execute(
                "INSERT INTO conversations (id, event_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                (event["id"], event["id"]),
            )
            friends = connection.execute("""SELECT CASE WHEN requester_id = ? THEN addressee_id ELSE requester_id END AS user_id
                FROM friendships WHERE status = 'accepted' AND (requester_id = ? OR addressee_id = ?)""", (created_by, created_by, created_by)).fetchall()
            for friend in friends:
                notification_id = secrets.token_urlsafe(12)
                connection.execute(
                    "INSERT INTO notifications (id, user_id, kind, body, data) VALUES (?, ?, 'event_posted', ?, ?)",
                    (notification_id, friend["user_id"], "A friend posted a new event", json.dumps({"event_id": event["id"], "title": event["title"], "user_id": created_by})),
                )
        return {**event, "starts_at": starts_at, "ends_at": ends_at, "created_by": created_by}

    def update_event(self, event_id: str, updates: dict[str, Any], user_id: str) -> dict[str, Any] | None:
        allowed = {key: updates[key] for key in ("title", "category", "when", "where", "desc", "lat", "lng", "starts_at", "ends_at", "chat_icon") if key in updates}
        if not allowed:
            return None
        assignments = ", ".join(f"{key if key not in {'when','where','desc'} else {'when':'event_when','where':'location','desc':'description'}[key]} = ?" for key in allowed)
        values = [allowed[key] for key in allowed] + [event_id, user_id]
        with self._connect() as connection:
            cursor = connection.execute(f"""UPDATE events SET {assignments} WHERE id = ? AND (
                created_by = ? OR EXISTS (SELECT 1 FROM event_admins WHERE event_id = events.id AND user_id = ?)
            )""", [*allowed.values(), event_id, user_id, user_id])
            if cursor.rowcount == 0:
                return None
        return next((event for event in self.list_events() if event["id"] == event_id), None)

    def delete_event(self, event_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("""DELETE FROM events WHERE id = ? AND (
                created_by = ? OR EXISTS (SELECT 1 FROM event_admins WHERE event_id = events.id AND user_id = ?)
            )""", (event_id, user_id, user_id))
        return cursor.rowcount > 0

    def promote_admin(self, event_id: str, user_id: str, owner_id: str) -> bool:
        with self._connect() as connection:
            if connection.execute("SELECT 1 FROM events WHERE id = ? AND created_by = ?", (event_id, owner_id)).fetchone() is None:
                return False
            if connection.execute("SELECT 1 FROM event_rsvps WHERE event_id = ? AND user_id = ?", (event_id, user_id)).fetchone() is None:
                return False
            connection.execute("INSERT INTO event_admins (event_id, user_id) VALUES (?, ?) ON CONFLICT DO NOTHING", (event_id, user_id))
        return True

    def remove_admin(self, event_id: str, user_id: str, owner_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute("""DELETE FROM event_admins WHERE event_id = ? AND user_id = ?
                AND EXISTS (SELECT 1 FROM events WHERE id = ? AND created_by = ?)""", (event_id, user_id, event_id, owner_id))
        return cursor.rowcount > 0

    def list_admins(self, event_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT u.id, u.name, u.photo FROM event_admins a
                JOIN users u ON u.id = a.user_id WHERE a.event_id = ? ORDER BY u.name""", (event_id,)).fetchall()
        return [dict(row) for row in rows]

    def list_attendees(self, event_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT u.id, u.name, u.photo, u.school, u.year
                FROM event_rsvps r JOIN users u ON u.id = r.user_id
                WHERE r.event_id = ? ORDER BY u.name""", (event_id,)).fetchall()
        return [dict(row) for row in rows]

    def send_friend_request(self, requester_id: str, addressee_id: str) -> dict[str, Any]:
        if requester_id == addressee_id:
            raise ValueError("cannot friend yourself")
        with self._connect() as connection:
            connection.execute("""INSERT INTO friendships (requester_id, addressee_id, status) VALUES (?, ?, 'pending')
                ON CONFLICT (requester_id, addressee_id) DO UPDATE SET status = 'pending'""", (requester_id, addressee_id))
            notification_id = secrets.token_urlsafe(12)
            connection.execute("INSERT INTO notifications (id, user_id, kind, body, data) VALUES (?, ?, 'friend_request', ?, ?)", (notification_id, addressee_id, "You have a new friend request", json.dumps({"user_id": requester_id})))
        return {"requester_id": requester_id, "addressee_id": addressee_id, "status": "pending"}

    def respond_friend_request(self, requester_id: str, addressee_id: str, status: str) -> bool:
        if status not in {"accepted", "declined"}:
            return False
        with self._connect() as connection:
            cursor = connection.execute("UPDATE friendships SET status = ? WHERE requester_id = ? AND addressee_id = ? AND status = 'pending'", (status, requester_id, addressee_id))
            if cursor.rowcount and status == "accepted":
                notification_id = secrets.token_urlsafe(12)
                connection.execute("INSERT INTO notifications (id, user_id, kind, body, data) VALUES (?, ?, 'friend_accepted', ?, ?)", (notification_id, requester_id, "Your friend request was accepted", json.dumps({"user_id": addressee_id})))
        return cursor.rowcount > 0

    def list_friends(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT u.id, u.name, u.photo FROM users u JOIN friendships f ON
                ((f.requester_id = u.id AND f.addressee_id = ?) OR (f.addressee_id = u.id AND f.requester_id = ?))
                WHERE f.status = 'accepted' ORDER BY u.name""", (user_id, user_id)).fetchall()
        return [dict(row) for row in rows]

    def list_friend_requests(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT u.id, u.name, u.photo FROM friendships f
                JOIN users u ON u.id = f.requester_id
                WHERE f.addressee_id = ? AND f.status = 'pending' ORDER BY f.created_at DESC""", (user_id,)).fetchall()
        return [dict(row) for row in rows]

    def send_direct_message(self, sender_id: str, recipient_id: str, body: str, kind: str = "text", attachment_url: str | None = None) -> dict[str, Any] | None:
        with self._connect() as connection:
            if connection.execute("""SELECT 1 FROM friendships WHERE status = 'accepted' AND
                ((requester_id = ? AND addressee_id = ?) OR (requester_id = ? AND addressee_id = ?))""", (sender_id, recipient_id, recipient_id, sender_id)).fetchone() is None:
                return None
            message_id = secrets.token_urlsafe(12)
            connection.execute("INSERT INTO direct_messages (id, sender_id, recipient_id, body, kind, attachment_url) VALUES (?, ?, ?, ?, ?, ?)", (message_id, sender_id, recipient_id, body.strip(), kind, attachment_url))
            notification_id = secrets.token_urlsafe(12)
            connection.execute("INSERT INTO notifications (id, user_id, kind, body, data) VALUES (?, ?, 'direct_message', ?, ?)", (notification_id, recipient_id, "You have a new message", json.dumps({"sender_id": sender_id, "message_id": message_id})))
            row = connection.execute("SELECT id, sender_id, recipient_id, body, kind, attachment_url, created_at FROM direct_messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row)

    def list_notifications(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, kind, body, data, read_at, created_at FROM notifications WHERE user_id = ? ORDER BY created_at DESC LIMIT 100", (user_id,)).fetchall()
        return [{**dict(row), "data": json.loads(row["data"])} for row in rows]

    def list_direct_messages(self, user_id: str, friend_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT id, sender_id, recipient_id, body, kind, attachment_url, created_at
                FROM direct_messages WHERE (sender_id = ? AND recipient_id = ?)
                OR (sender_id = ? AND recipient_id = ?) ORDER BY created_at ASC LIMIT 200""",
                (user_id, friend_id, friend_id, user_id)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _serialize(row: sqlite3.Row, rsvps: list[str]) -> dict[str, Any]:
        people = json.loads(row["people"])
        for user_id in rsvps:
            if user_id not in people:
                people.append(user_id)
        return {"id": row["id"], "title": row["title"], "category": row["category"],
                "when": row["event_when"], "where": row["location"], "desc": row["description"],
                "people": people, "lat": row["lat"], "lng": row["lng"],
                "starts_at": row["starts_at"], "ends_at": row["ends_at"],
                "created_by": row["created_by"], "chat_icon": row["chat_icon"]}
