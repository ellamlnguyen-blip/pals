"""Run a non-destructive PostgreSQL persistence smoke test.

Usage: DATABASE_URL=postgresql://... python3 scripts/verify_postgres.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.storage import EventStore

if not os.getenv("DATABASE_URL", "").startswith(("postgres://", "postgresql://")):
    raise SystemExit("DATABASE_URL must be a PostgreSQL connection string")

store = EventStore(Path("/tmp/pals-postgres-smoke.db"))
store.initialize([])
print("PostgreSQL schema initialized successfully")
