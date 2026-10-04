"""Set the Free product cap to 10 and the Starter cap to 50."""

from app.security import iso_utc


def upgrade(db):
    now = iso_utc()
    db.execute("UPDATE plans SET product_limit=?,updated_at=? WHERE id='FREE'", (10, now))
    db.execute("UPDATE plans SET product_limit=?,updated_at=? WHERE id='STARTER'", (50, now))
