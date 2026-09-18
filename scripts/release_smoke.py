"""Run a small authenticated smoke test against a deployed Pals API.

Usage: PALS_API_URL=https://api.example.com python3 scripts/release_smoke.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request


base = os.getenv("PALS_API_URL", "").rstrip("/")
if not base:
    raise SystemExit("Set PALS_API_URL to the deployed API origin")
api = f"{base}/api/v1"
email = f"release-smoke-{int(time.time())}@example.com"


def request(path: str, method: str = "GET", payload: dict | None = None, token: str | None = None):
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if body else {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request_obj = urllib.request.Request(f"{api}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request_obj, timeout=15) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"{method} {path} returned {error.code}: {detail}") from error


with urllib.request.urlopen(f"{base}/healthz", timeout=15) as health_response:
    status = health_response.status
    health = json.loads(health_response.read())
if status != 200 or health.get("status") != "ready":
    raise SystemExit(f"Backend is not ready: {health}")

_, account = request("/auth/register", "POST", {"email": email, "name": "Release Smoke", "password": "password123"})
_, session = request("/auth/login", "POST", {"email": email, "password": "password123"})
token = session["token"]
event_id = f"release-smoke-{int(time.time())}"
event = {"id": event_id, "title": "Release smoke event", "category": "test", "when": "Now", "where": "Test", "desc": "Temporary release check", "people": [], "lat": 0, "lng": 0}
request("/events", "POST", event, token)
request(f"/events/{event_id}/rsvps", "POST", token=token)
request(f"/events/{event_id}", "DELETE", token=token)
print(f"Release smoke test passed for {account['email']}")
