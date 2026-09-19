import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.storage import EventStore


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        main._rate_limits.clear()
        main.event_store = EventStore(Path(self.temp_dir.name) / "api.db")
        main.event_store.initialize([])
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.temp_dir.cleanup()

    def register(self, email="ella@example.com"):
        response = self.client.post("/api/v1/auth/register", json={"email": email, "name": "Ella", "password": "password123"})
        self.assertEqual(response.status_code, 201, response.text)
        login = self.client.post("/api/v1/auth/login", json={"email": email, "password": "password123"})
        self.assertEqual(login.status_code, 200, login.text)
        return {"Authorization": f"Bearer {login.json()['token']}"}

    def test_protected_event_and_rsvp_flow(self):
        unauthenticated_event = {"id": "blocked", "title": "Blocked", "category": "fun", "when": "Tomorrow", "where": "Campus", "desc": "Test", "lat": 1, "lng": 2}
        self.assertEqual(self.client.post("/api/v1/events", json=unauthenticated_event).status_code, 401)
        headers = self.register()
        event = {"id": "api-event", "title": "API Event", "category": "fun", "when": "Tomorrow", "where": "Campus", "desc": "Test", "lat": 1, "lng": 2}
        created = self.client.post("/api/v1/events", json=event, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(self.client.post("/api/v1/events/api-event/rsvps", headers=headers).status_code, 201)
        self.assertEqual(self.client.post("/api/v1/events/api-event/rsvps", headers=headers).status_code, 409)
        message = self.client.post("/api/v1/events/api-event/messages", json={"body": "Hello"}, headers=headers)
        self.assertEqual(message.status_code, 201, message.text)
        self.assertEqual(self.client.get("/api/v1/events/api-event/messages", headers=headers).json()[0]["body"], "Hello")

        token = headers["Authorization"].split(" ", 1)[1]
        with self.client.websocket_connect(f"/api/v1/events/api-event/messages/ws?token={token}") as websocket:
            websocket.send_json({"body": "Hello over WebSocket"})
            socket_message = websocket.receive_json()
        self.assertEqual(socket_message["body"], "Hello over WebSocket")
        history = self.client.get("/api/v1/events/api-event/messages", headers=headers).json()
        self.assertTrue(any(item["body"] == "Hello over WebSocket" for item in history))

    def test_event_rejects_invalid_schedule_and_unsafe_id(self):
        headers = self.register("schedule@example.com")
        invalid_schedule = {"id": "schedule-event", "title": "Test", "category": "fun", "when": "Tomorrow", "where": "Campus", "desc": "Test", "lat": 1, "lng": 2, "starts_at": "2026-09-20T10:00:00+00:00", "ends_at": "2026-09-20T09:00:00+00:00"}
        self.assertEqual(self.client.post("/api/v1/events", json=invalid_schedule, headers=headers).status_code, 422)
        invalid_schedule["id"] = "bad id"
        invalid_schedule["ends_at"] = "2026-09-20T11:00:00+00:00"
        self.assertEqual(self.client.post("/api/v1/events", json=invalid_schedule, headers=headers).status_code, 422)

    def test_logout_invalidates_session_and_readiness_is_available(self):
        headers = self.register("logout@example.com")
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 200)
        self.assertEqual(self.client.post("/api/v1/auth/logout", headers=headers).status_code, 204)
        self.assertEqual(self.client.get("/api/v1/auth/me", headers=headers).status_code, 401)
        readiness = self.client.get("/healthz")
        self.assertEqual(readiness.json(), {"status": "ready"})
        self.assertEqual(readiness.headers["x-content-type-options"], "nosniff")
        self.assertEqual(readiness.headers["x-frame-options"], "DENY")

    def test_media_validation_requires_auth_and_rejects_unsupported_types(self):
        response = self.client.post("/api/v1/media", files={"file": ("note.txt", b"hello", "text/plain")})
        self.assertEqual(response.status_code, 401)
        headers = self.register("media@example.com")
        response = self.client.post("/api/v1/media", files={"file": ("note.txt", b"hello", "text/plain")}, headers=headers)
        self.assertEqual(response.status_code, 415)

    def test_friend_direct_photo_message_round_trip(self):
        sender_headers = self.register("sender@example.com")
        recipient_headers = self.register("recipient@example.com")
        recipient = self.client.get("/api/v1/auth/me", headers=recipient_headers).json()
        sender = self.client.get("/api/v1/auth/me", headers=sender_headers).json()
        self.assertEqual(self.client.post(f"/api/v1/friends/{recipient['id']}", headers=sender_headers).status_code, 201)
        self.assertEqual(self.client.post(f"/api/v1/friends/{sender['id']}/response", json={"status": "accepted"}, headers=recipient_headers).status_code, 200)
        sent = self.client.post(
            f"/api/v1/users/{recipient['id']}/messages",
            json={"body": "photo", "kind": "photo", "attachment_url": "/media/example.jpg"},
            headers=sender_headers,
        )
        self.assertEqual(sent.status_code, 201, sent.text)
        self.assertEqual(sent.json()["kind"], "photo")
        history = self.client.get(f"/api/v1/users/{sender['id']}/messages", headers=recipient_headers)
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(history.json()[0]["attachment_url"], "/media/example.jpg")

    def test_profile_gallery_round_trip_is_limited_to_four(self):
        headers = self.register()
        gallery = ["/media/one.jpg", "/media/two.jpg", "/media/three.jpg", "/media/four.jpg"]
        response = self.client.patch("/api/v1/auth/me/profile", json={"name": "Test User", "photo_gallery": gallery, "instagram": "@testuser"}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["photo_gallery"], gallery)
        self.assertEqual(response.json()["instagram"], "@testuser")
        too_many = self.client.patch("/api/v1/auth/me/profile", json={"name": "Test User", "photo_gallery": gallery + ["/media/five.jpg"]}, headers=headers)
        self.assertEqual(too_many.status_code, 422)
if __name__ == "__main__":
    unittest.main()
