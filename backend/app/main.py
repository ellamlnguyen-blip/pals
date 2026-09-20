from __future__ import annotations

import os
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Header, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .storage import EventStore

app = FastAPI(title="Pals API")
APP_RELEASE = "account-rollout-v1"
MEDIA_DIR = Path(os.getenv("PALS_MEDIA_DIR", str(Path(__file__).resolve().parent.parent / "media")))
MEDIA_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=MEDIA_DIR), name="media")

_rate_limits: dict[tuple[str, str], list[float]] = {}


def is_unique_violation(error: Exception) -> bool:
    message = str(error).lower()
    return "unique constraint failed" in message or "duplicate key value" in message or "uniqueviolation" in message


@app.middleware("http")
async def abuse_protection(request: Request, call_next):
    if request.method in {"POST", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        bucket = "auth" if "/auth/" in request.url.path else "write"
        limit = 10 if bucket == "auth" else 120
        key = (request.client.host if request.client else "unknown", bucket)
        now = time.monotonic()
        recent = [stamp for stamp in _rate_limits.get(key, []) if now - stamp < 60]
        if len(recent) >= limit:
            return JSONResponse({"detail": "Too many requests"}, status_code=429, headers={"Retry-After": "60"})
        recent.append(now)
        _rate_limits[key] = recent
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in os.getenv("PALS_ALLOWED_ORIGINS", "").split(",") if origin.strip()],
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SAMPLE_EVENTS = [
    {
        "id": "picnic",
        "title": "Sunset picnic at the arboretum",
        "category": "chilling",
        "when": "Today at 6:30 PM",
        "where": "Coker Arboretum",
        "desc": "Bring a blanket or something easy to share. We will grab a shady spot, listen to music, and stay through sunset.",
        "people": ["maya", "jordan", "theo"],
        "lat": 35.9149,
        "lng": -79.0482,
    },
    {
        "id": "ceramics",
        "title": "Ceramics and coffee",
        "category": "fun",
        "when": "Tomorrow at 11:00 AM",
        "where": "Student Union art studio",
        "desc": "A beginner-friendly clay session followed by coffee nearby. No experience needed and all supplies are available.",
        "people": ["priya", "lena"],
        "lat": 35.9109,
        "lng": -79.0471,
    },
    {
        "id": "volleyball",
        "title": "Pickup volleyball",
        "category": "active",
        "when": "Thursday at 5:00 PM",
        "where": "Hooker Fields",
        "desc": "Casual mixed-skill volleyball. We will make teams when everyone arrives. Bring water and come ready to rotate often.",
        "people": ["jordan", "devon", "maya", "theo"],
        "lat": 35.9056,
        "lng": -79.0452,
    },
]

event_store = EventStore()


class ChatConnections:
    def __init__(self) -> None:
        self.connections: dict[str, set[WebSocket]] = {}

    async def connect(self, event_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.setdefault(event_id, set()).add(websocket)

    def disconnect(self, event_id: str, websocket: WebSocket) -> None:
        self.connections.get(event_id, set()).discard(websocket)

    async def broadcast(self, event_id: str, message: dict) -> None:
        for connection in list(self.connections.get(event_id, set())):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(event_id, connection)


chat_connections = ChatConnections()


class Event(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=160)
    category: str = Field(min_length=1, max_length=40)
    when: str = Field(min_length=1, max_length=160)
    where: str = Field(min_length=1, max_length=240)
    desc: str = Field(min_length=1, max_length=4000)
    people: list[str] = Field(default_factory=list)
    lat: float
    lng: float
    starts_at: Optional[str] = None
    ends_at: Optional[str] = None
    chat_icon: Optional[str] = Field(default=None, max_length=8)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=8)


class MessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="text", pattern="^(text|photo|emoji|sticker|gif)$")
    attachment_url: Optional[str] = None


class ProfileRequest(BaseModel):
    name: str = Field(min_length=1)
    photo: Optional[str] = None
    photo_gallery: list[str] = Field(default_factory=list, max_length=4)
    photo_position_x: float = Field(default=50, ge=0, le=100)
    photo_position_y: float = Field(default=50, ge=0, le=100)
    photo_zoom: float = Field(default=1, ge=1, le=3)
    instagram: Optional[str] = Field(default=None, max_length=80)
    school: Optional[str] = None
    year: Optional[str] = None
    major: Optional[str] = None
    hometown: Optional[str] = None
    bio: Optional[str] = None
    hobbies: list[str] = Field(default_factory=list)
    goals: Optional[str] = None
    friend_activities: Optional[str] = None
    fun_facts: Optional[str] = None
    things_to_do: Optional[str] = None
    favorite_foods: Optional[str] = None
    favorite_music: Optional[str] = None


class AdminRequest(BaseModel):
    user_id: str = Field(min_length=1)


class FriendResponse(BaseModel):
    status: str


class DirectMessageRequest(BaseModel):
    body: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default="text", pattern="^(text|photo|emoji|sticker|gif)$")
    attachment_url: Optional[str] = None


@app.on_event("startup")
def initialize_storage() -> None:
    seed_events = SAMPLE_EVENTS if os.getenv("PALS_SEED_SAMPLE_EVENTS", "false").lower() == "true" else []
    event_store.initialize(seed_events)


@app.get("/")
def health_check() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/healthz")
def readiness_check(response: Response) -> dict[str, str]:
    response.headers["X-Pals-Release"] = APP_RELEASE
    response.headers["X-Pals-Storage"] = "postgres" if event_store.uses_postgres else "sqlite"
    return {"status": "ready"}


@app.post("/api/v1/auth/register", status_code=201)
def register(request: RegisterRequest) -> dict:
    try:
        return event_store.create_user(request.email, request.name, request.password)
    except Exception as error:
        if is_unique_violation(error):
            raise HTTPException(status_code=409, detail="An account with this email already exists") from error
        raise


@app.post("/api/v1/auth/login")
def login(request: LoginRequest) -> dict:
    session = event_store.authenticate(request.email, request.password)
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return session


@app.post("/api/v1/auth/logout", status_code=204)
def logout(authorization: Optional[str] = Header(default=None)) -> None:
    if not authorization or not authorization.lower().startswith("bearer "):
        return
    event_store.delete_session(authorization[7:].strip())


@app.post("/api/v1/auth/change-password")
def change_password(request: ChangePasswordRequest, authorization: Optional[str] = Header(default=None)) -> dict[str, str]:
    user = current_user(authorization)
    if not event_store.change_password(user["id"], request.current_password, request.new_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    return {"status": "updated"}


def current_user(authorization: Optional[str]) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")
    user = event_store.get_user_by_token(authorization[7:].strip())
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def validate_event_schedule(event: Event) -> None:
    if not event.starts_at or not event.ends_at:
        return
    try:
        starts_at = datetime.fromisoformat(event.starts_at.replace("Z", "+00:00"))
        ends_at = datetime.fromisoformat(event.ends_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise HTTPException(status_code=422, detail="starts_at and ends_at must be ISO timestamps") from error
    if ends_at <= starts_at:
        raise HTTPException(status_code=422, detail="ends_at must be after starts_at")


@app.get("/api/v1/auth/me")
def me(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
    return current_user(authorization)


@app.get("/api/v1/auth/me/rsvps")
def my_rsvps(authorization: Optional[str] = Header(default=None)) -> dict[str, list[str]]:
    user = current_user(authorization)
    return {"event_ids": event_store.list_rsvps(user["id"])}


@app.patch("/api/v1/auth/me/profile")
def update_my_profile(profile: ProfileRequest, authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    payload = profile.model_dump() if hasattr(profile, "model_dump") else profile.dict()
    return event_store.update_profile(user["id"], payload) or {}


@app.get("/api/v1/auth/me/profile")
def get_my_profile(authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    return event_store.get_profile(user["id"]) or {}


@app.get("/api/v1/people")
def list_people() -> list[dict]:
    return event_store.list_profiles()


@app.get("/api/v1/people/{user_id}")
def get_person(user_id: str) -> dict:
    profile = event_store.get_profile(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile


@app.post("/api/v1/friends/{user_id}", status_code=201)
def friend_request(user_id: str, authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    try:
        return event_store.send_friend_request(user["id"], user_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@app.post("/api/v1/friends/{user_id}/response")
def respond_friend(user_id: str, response: FriendResponse, authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    if not event_store.respond_friend_request(user_id, user["id"], response.status):
        raise HTTPException(status_code=404, detail="Friend request not found")
    return {"user_id": user_id, "status": response.status}


@app.get("/api/v1/friends")
def list_friends(authorization: Optional[str] = Header(default=None)) -> list[dict]:
    return event_store.list_friends(current_user(authorization)["id"])


@app.get("/api/v1/friends/requests")
def friend_requests(authorization: Optional[str] = Header(default=None)) -> list[dict]:
    return event_store.list_friend_requests(current_user(authorization)["id"])


@app.get("/api/v1/notifications")
def notifications(authorization: Optional[str] = Header(default=None)) -> list[dict]:
    return event_store.list_notifications(current_user(authorization)["id"])


@app.post("/api/v1/media", status_code=201)
async def upload_media(file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)) -> dict[str, str]:
    current_user(authorization)
    allowed_types = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp"}
    if file.content_type not in allowed_types:
        raise HTTPException(status_code=415, detail="Only JPEG, PNG, GIF, and WebP images are supported")
    content = await file.read(10 * 1024 * 1024 + 1)
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image must be 10 MB or smaller")
    filename = f"{secrets.token_urlsafe(18)}{allowed_types[file.content_type]}"
    bucket = os.getenv("PALS_S3_BUCKET")
    if bucket:
        import boto3
        client = boto3.client(
            "s3",
            region_name=os.getenv("PALS_S3_REGION", "us-east-1"),
            endpoint_url=os.getenv("PALS_S3_ENDPOINT_URL") or None,
            aws_access_key_id=os.getenv("PALS_S3_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("PALS_S3_SECRET_ACCESS_KEY"),
        )
        client.put_object(Bucket=bucket, Key=filename, Body=content, ContentType=file.content_type, CacheControl="public, max-age=31536000")
        public_base = os.getenv("PALS_MEDIA_PUBLIC_URL", "").rstrip("/")
        url = f"{public_base}/{filename}" if public_base else f"{os.getenv('PALS_S3_ENDPOINT_URL', '').rstrip('/')}/{bucket}/{filename}"
    else:
        (MEDIA_DIR / filename).write_bytes(content)
        url = f"/media/{filename}"
    return {"url": url, "content_type": file.content_type}


@app.post("/api/v1/notifications/{notification_id}/read", status_code=204)
def mark_notification_read(notification_id: str, authorization: Optional[str] = Header(default=None)) -> None:
    if not event_store.mark_notification_read(notification_id, current_user(authorization)["id"]):
        raise HTTPException(status_code=404, detail="Notification not found")


@app.post("/api/v1/users/{user_id}/messages", status_code=201)
def direct_message(user_id: str, message: DirectMessageRequest, authorization: Optional[str] = Header(default=None)) -> dict:
    sender = current_user(authorization)
    created = event_store.send_direct_message(sender["id"], user_id, message.body, message.kind, message.attachment_url)
    if created is None:
        raise HTTPException(status_code=403, detail="You can only message accepted friends")
    return created


@app.get("/api/v1/users/{user_id}/messages")
def direct_messages(user_id: str, authorization: Optional[str] = Header(default=None)) -> list[dict]:
    current = current_user(authorization)
    if user_id not in {friend["id"] for friend in event_store.list_friends(current["id"])}:
        raise HTTPException(status_code=403, detail="You can only view messages with accepted friends")
    return event_store.list_direct_messages(current["id"], user_id)


@app.get("/api/v1/events")
def list_events() -> list[dict]:
    return [
        event for event in event_store.list_events()
        if not str(event.get("id", "")).startswith(("release-smoke-", "release-debug-"))
    ]


@app.get("/api/v1/calendar")
def calendar_events() -> list[dict]:
    """Return the current internal calendar, excluding events 24h after they start."""
    return event_store.list_events()


@app.post("/api/v1/events", status_code=201)
def create_event(event: Event, authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    validate_event_schedule(event)
    try:
        payload = event.model_dump() if hasattr(event, "model_dump") else event.dict()
        return event_store.create_event(payload, user["id"])
    except Exception as error:
        if is_unique_violation(error):
            raise HTTPException(status_code=409, detail="An event with this id already exists") from error
        raise


@app.patch("/api/v1/events/{event_id}")
def update_event(event_id: str, event: Event, authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    validate_event_schedule(event)
    updated = event_store.update_event(event_id, event.model_dump() if hasattr(event, "model_dump") else event.dict(), user["id"])
    if updated is None:
        raise HTTPException(status_code=404, detail="Event not found or not owned by you")
    return updated


@app.delete("/api/v1/events/{event_id}", status_code=204)
def delete_event(event_id: str, authorization: Optional[str] = Header(default=None)) -> None:
    user = current_user(authorization)
    if not event_store.delete_event(event_id, user["id"]):
        raise HTTPException(status_code=404, detail="Event not found or not owned by you")


@app.get("/api/v1/events/{event_id}/admins")
def list_event_admins(event_id: str) -> list[dict]:
    return event_store.list_admins(event_id)


@app.get("/api/v1/events/{event_id}/attendees")
def list_event_attendees(event_id: str, authorization: Optional[str] = Header(default=None)) -> list[dict]:
    current_user(authorization)
    return event_store.list_attendees(event_id)


@app.post("/api/v1/events/{event_id}/admins", status_code=201)
def promote_event_admin(event_id: str, request: AdminRequest, authorization: Optional[str] = Header(default=None)) -> dict:
    owner = current_user(authorization)
    if not event_store.promote_admin(event_id, request.user_id, owner["id"]):
        raise HTTPException(status_code=403, detail="Only the event owner can promote an RSVPed user")
    return {"event_id": event_id, "user_id": request.user_id, "role": "admin"}


@app.delete("/api/v1/events/{event_id}/admins/{user_id}", status_code=204)
def remove_event_admin(event_id: str, user_id: str, authorization: Optional[str] = Header(default=None)) -> None:
    owner = current_user(authorization)
    if not event_store.remove_admin(event_id, user_id, owner["id"]):
        raise HTTPException(status_code=404, detail="Admin not found or you are not the event owner")


@app.get("/api/v1/events/{event_id}/messages")
def list_messages(event_id: str, limit: int = 50, before: Optional[str] = None, authorization: Optional[str] = Header(default=None)) -> list[dict]:
    user = current_user(authorization)
    if not event_store.can_message_event(event_id, user["id"]):
        raise HTTPException(status_code=403, detail="RSVP to this event to access its chat")
    try:
        return event_store.list_messages(event_id, limit, before)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Event not found") from error


@app.post("/api/v1/events/{event_id}/messages", status_code=201)
async def create_message(event_id: str, message: MessageRequest, authorization: Optional[str] = Header(default=None)) -> dict:
    user = current_user(authorization)
    if not event_store.can_message_event(event_id, user["id"]):
        raise HTTPException(status_code=403, detail="RSVP to this event to access its chat")
    try:
        created = event_store.create_message(event_id, user["id"], message.body, message.kind, message.attachment_url)
        await chat_connections.broadcast(event_id, created)
        return created
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Event not found") from error


@app.delete("/api/v1/messages/{message_id}", status_code=204)
def delete_message(message_id: str, authorization: Optional[str] = Header(default=None)) -> None:
    user = current_user(authorization)
    if not event_store.delete_message(message_id, user["id"]):
        raise HTTPException(status_code=404, detail="Message not found or not owned by you")


@app.websocket("/api/v1/events/{event_id}/messages/ws")
async def message_socket(websocket: WebSocket, event_id: str, token: Optional[str] = None) -> None:
    user = event_store.get_user_by_token(token) if token else None
    if user is None or not event_store.can_message_event(event_id, user["id"]):
        await websocket.close(code=1008)
        return
    await chat_connections.connect(event_id, websocket)
    try:
        while True:
            payload = await websocket.receive_json()
            body = str(payload.get("body", "")).strip()
            if not body or len(body) > 2000:
                await websocket.send_json({"error": "Message must be between 1 and 2000 characters"})
                continue
            message = event_store.create_message(event_id, user["id"], body)
            await chat_connections.broadcast(event_id, message)
    except WebSocketDisconnect:
        chat_connections.disconnect(event_id, websocket)


@app.post("/api/v1/events/{event_id}/rsvps", status_code=201)
def rsvp_to_event(event_id: str, authorization: Optional[str] = Header(default=None)) -> dict[str, str]:
    user = current_user(authorization)
    try:
        return event_store.rsvp(event_id, user["id"])
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Event not found") from error
    except Exception as error:
        if is_unique_violation(error):
            raise HTTPException(status_code=409, detail="User is already going to this event") from error
        raise


@app.delete("/api/v1/events/{event_id}/rsvps", status_code=204)
def cancel_rsvp(event_id: str, authorization: Optional[str] = Header(default=None)) -> None:
    user = current_user(authorization)
    try:
        event_store.cancel_rsvp(event_id, user["id"])
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Event not found") from error
