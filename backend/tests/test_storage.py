import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.storage import EventStore


def event(event_id="event-1", starts_at=None):
    return {
        "id": event_id, "title": "Campus walk", "category": "active",
        "when": "Today at 5 PM", "where": "Campus", "desc": "Walk",
        "people": [], "lat": 35.9, "lng": -79.0,
        "starts_at": starts_at or (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "ends_at": (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat(),
    }


class EventStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = EventStore(Path(self.temp_dir.name) / "test.db")
        self.store.initialize([])

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_auth_and_rsvp_lifecycle(self):
        user = self.store.create_user("ella@example.com", "Ella", "password123")
        session = self.store.authenticate("ella@example.com", "password123")
        self.assertIsNotNone(session)
        created = self.store.create_event(event(), user["id"])
        self.assertEqual(self.store.rsvp(created["id"], user["id"])["status"], "going")
        self.assertIn(created["id"], self.store.list_rsvps(user["id"]))
        self.store.cancel_rsvp(created["id"], user["id"])
        self.assertNotIn(created["id"], self.store.list_rsvps(user["id"]))

    def test_expired_events_are_hidden(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.store.create_event(event("expired", old), "owner")
        self.assertEqual(self.store.list_events(), [])

    def test_owner_can_update_and_delete(self):
        self.store.create_event(event(), "owner")
        self.assertIsNotNone(self.store.update_event("event-1", {"title": "Updated"}, "owner"))
        self.assertFalse(self.store.delete_event("event-1", "another-user"))
        self.assertTrue(self.store.delete_event("event-1", "owner"))

    def test_event_listing_preserves_owner_for_client_controls(self):
        owner = self.store.create_user("owner-list@example.com", "Owner", "password123")
        self.store.create_event(event("owner-visible"), owner["id"])
        listed = self.store.list_events()
        self.assertEqual(listed[0]["created_by"], owner["id"])

    def test_event_chat_requires_rsvp_and_persists_messages(self):
        owner = self.store.create_user("owner@example.com", "Owner", "password123")
        guest = self.store.create_user("guest@example.com", "Guest", "password123")
        created = self.store.create_event(event(), owner["id"])
        self.store.rsvp(created["id"], guest["id"])
        self.assertTrue(self.store.can_message_event(created["id"], guest["id"]))
        message = self.store.create_message(created["id"], guest["id"], "Hello everyone")
        self.assertEqual(self.store.list_messages(created["id"])[0]["body"], "Hello everyone")
        self.assertTrue(self.store.delete_message(message["id"], guest["id"]))
        self.assertEqual(self.store.list_messages(created["id"]), [])

    def test_event_creation_creates_its_own_conversation(self):
        owner = self.store.create_user("conversation-owner@example.com", "Owner", "password123")
        created = self.store.create_event(event("conversation-event"), owner["id"])
        with self.store._connect() as connection:
            conversation = connection.execute("SELECT event_id FROM conversations WHERE id = ?", (created["id"],)).fetchone()
        self.assertIsNotNone(conversation)
        self.assertEqual(conversation["event_id"], created["id"])

    def test_synthetic_release_smoke_accounts_are_removed_on_initialize(self):
        account = self.store.create_user("release-smoke-cleanup@example.com", "Release Smoke", "password123")
        self.store.create_event(event("release-smoke-cleanup-event"), account["id"])
        self.store.initialize([])
        self.assertEqual(self.store.list_profiles(), [])
        self.assertFalse(any(item["id"] == "release-smoke-cleanup-event" for item in self.store.list_events()))

    def test_reserved_release_artifacts_are_never_listed(self):
        self.store.create_event(event("release-smoke-visible-check"), "owner")
        self.store.create_event(event("release-debug-visible-check"), "owner")
        self.store.create_event(event("real-visible-check"), "owner")
        listed_ids = {item["id"] for item in self.store.list_events()}
        self.assertEqual(listed_ids, {"real-visible-check"})

    def test_event_notifications_cover_posting_and_attendance(self):
        owner = self.store.create_user("notify-owner@example.com", "Owner", "password123")
        friend = self.store.create_user("notify-friend@example.com", "Friend", "password123")
        self.store.send_friend_request(owner["id"], friend["id"])
        self.store.respond_friend_request(owner["id"], friend["id"], "accepted")
        created = self.store.create_event(event("notify-event"), owner["id"])
        posted = self.store.list_notifications(friend["id"])
        self.assertTrue(any(item["kind"] == "event_posted" for item in posted))
        self.store.rsvp(created["id"], friend["id"])
        attendance = self.store.list_notifications(owner["id"])
        self.assertTrue(any(item["kind"] == "event_attendance" for item in attendance))


if __name__ == "__main__":
    unittest.main()
