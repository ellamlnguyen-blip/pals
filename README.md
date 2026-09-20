# Pals

Pals is a campus events, friendships, profiles, calendar, RSVP, and messaging app.

## Local development

```bash
python3 -m pip install -r requirements.txt
cd backend
python3 -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `index.html` in a browser. The frontend uses `config.js` for the API base URL.

Run the backend tests from the repository root:

```bash
PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
```

When a PostgreSQL connection is available, run the database smoke test too:

```bash
DATABASE_URL=postgresql://... PYTHONPATH=backend python3 -m unittest discover -s backend/tests -v
```

## Production configuration

The backend reads configuration from environment variables. Copy `.env.example` into your deployment configuration and set real values; never commit credentials.

- `DATABASE_URL`: PostgreSQL connection string
- `PALS_REQUIRE_DATABASE_URL`: set to `true` in production so the API never falls back to per-instance SQLite
- `PALS_ALLOWED_ORIGINS`: comma-separated HTTPS frontend origins
- `PALS_SESSION_HOURS`: session lifetime
- `PALS_S3_*`: S3-compatible media storage credentials
- `PALS_MEDIA_PUBLIC_URL`: public media base URL

`render.yaml` defines a Render FastAPI service, managed PostgreSQL database, and a persistent disk for uploaded media. After creating or connecting the existing Render service to this Blueprint, sync it and confirm that the service environment contains the generated `DATABASE_URL`; the value must not be committed to Git. `PALS_REQUIRE_DATABASE_URL=true` intentionally makes readiness fail if that shared database is missing. Deploy the static frontend separately through Vercel; set `window.PALS_API_URL` in the deployed `config.js` to the public API URL. For multi-instance or CDN scale, replace the disk with the S3-compatible settings above.

Before launch, verify `/healthz`, registration/login, event creation, RSVP, messaging, media upload, WebSockets, and PostgreSQL migrations against the deployed services.

Run the repeatable authenticated smoke test after deployment:

```bash
PALS_API_URL=https://api.your-domain.example python3 scripts/release_smoke.py
```
