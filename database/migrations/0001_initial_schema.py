"""Initial BizFlow schema plus idempotent upgrades for existing databases."""

from app.db import apply_initial_schema


def upgrade(db):
    apply_initial_schema(db)
