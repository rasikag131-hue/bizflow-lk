from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException, Request

from app.config import settings
from app.db import connect, transaction
from app.security import iso_utc, parse_dt, utc_now


def enforce_rate_limit(request: Request, *, scope: str, subject: str,
                       limit: int, window_seconds: int) -> None:
    """Database-backed fixed-window limiter. The database stores only a keyed bucket hash."""
    client_ip = request.client.host if request.client else "unknown"
    material = f"{scope}:{client_ip}:{subject}".encode("utf-8", "replace")
    bucket_hash = hmac.new(settings.session_secret.encode("utf-8"), material, hashlib.sha256).hexdigest()
    now_dt = utc_now()
    now = iso_utc(now_dt)
    db = connect()
    try:
        with transaction(db):
            if getattr(db, "dialect", "sqlite") == "postgres":
                lock_id = int.from_bytes(hashlib.sha256(bucket_hash.encode()).digest()[:8], "big") & ((1 << 63) - 1)
                db.execute("SELECT pg_advisory_xact_lock(?)", (lock_id,))
            db.execute(
                "INSERT INTO rate_limit_buckets(bucket_hash,window_start,hit_count,updated_at) "
                "VALUES(?,?,0,?) ON CONFLICT(bucket_hash) DO NOTHING",
                (bucket_hash, now, now),
            )
            query = "SELECT window_start,hit_count FROM rate_limit_buckets WHERE bucket_hash=?"
            if getattr(db, "dialect", "sqlite") == "postgres":
                query += " FOR UPDATE"
            row = db.execute(query, (bucket_hash,)).fetchone()
            window_start = parse_dt(row["window_start"] if row else None)
            elapsed = (now_dt - window_start).total_seconds() if window_start else window_seconds
            count = int(row["hit_count"]) if row else 0
            if not window_start or elapsed >= window_seconds:
                db.execute("UPDATE rate_limit_buckets SET window_start=?,hit_count=1,updated_at=? WHERE bucket_hash=?",
                           (now, now, bucket_hash))
            elif count >= limit:
                retry_after = max(1, int(window_seconds - elapsed))
                raise HTTPException(status_code=429,
                                    detail="Too many attempts. Please wait before trying again.",
                                    headers={"Retry-After": str(retry_after)})
            else:
                db.execute("UPDATE rate_limit_buckets SET hit_count=hit_count+1,updated_at=? WHERE bucket_hash=?",
                           (now, bucket_hash))
            # Opportunistically bound old counters without adding a second scheduled job.
            if secrets.randbelow(128) == 0:
                cutoff = iso_utc(now_dt - timedelta(days=2))
                db.execute("DELETE FROM rate_limit_buckets WHERE updated_at<?", (cutoff,))
    finally:
        db.close()
