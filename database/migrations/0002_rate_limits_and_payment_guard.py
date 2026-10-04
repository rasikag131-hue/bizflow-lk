"""Persist rate-limit counters and prevent concurrent pending payments per business."""


def upgrade(db):
    duplicates = db.execute(
        "SELECT business_id FROM payment_requests WHERE status='PENDING' "
        "GROUP BY business_id HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone()
    if duplicates:
        raise RuntimeError(
            "More than one pending payment request exists for a business. Review and resolve those requests before migration 0002."
        )

    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_requests_one_pending_per_business "
        "ON payment_requests(business_id) WHERE status='PENDING'"
    )
    timestamp_type = "TIMESTAMPTZ" if getattr(db, "dialect", "sqlite") == "postgres" else "TEXT"
    db.execute(
        "CREATE TABLE IF NOT EXISTS rate_limit_buckets ("
        f"bucket_hash TEXT PRIMARY KEY, window_start {timestamp_type} NOT NULL, hit_count INTEGER NOT NULL, updated_at {timestamp_type} NOT NULL)"
    )
    db.execute("CREATE INDEX IF NOT EXISTS idx_rate_limit_buckets_updated ON rate_limit_buckets(updated_at)")
