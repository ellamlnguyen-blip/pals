import os
import tempfile
import unittest
import uuid
from pathlib import Path

from app.storage import EventStore


@unittest.skipUnless(os.getenv("DATABASE_URL", "").startswith(("postgres://", "postgresql://")), "DATABASE_URL is not configured for PostgreSQL")
class PostgresSmokeTests(unittest.TestCase):
    def test_schema_and_persistence(self):
        store = EventStore(Path(tempfile.gettempdir()) / "pals-postgres-fallback.db")
        store.initialize([])
        suffix = uuid.uuid4().hex
        user = store.create_user(f"postgres-smoke-{suffix}@example.com", "Postgres Smoke", "password123")
        event = {
            "id": f"postgres-smoke-event-{suffix}", "title": "Postgres smoke test", "category": "test",
            "when": "Now", "where": "Test", "desc": "Temporary", "people": [], "lat": 0, "lng": 0,
        }
        store.create_event(event, user["id"])
        self.assertEqual(store.list_events()[0]["id"], event["id"])
