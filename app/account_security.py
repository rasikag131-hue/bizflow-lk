from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import string
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.db import transaction
from app.security import clean_text


def normalize_identity_number(value: Any) -> str:
    raw = clean_text(value, limit=80, required=True, label="Identity Number")
    normalized = re.sub(r"[\s-]+", "", raw).upper()
    if not normalized:
        raise ValueError("Identity Number is required.")
    try:
        valid = re.fullmatch(settings.identity_number_regex, normalized, flags=re.IGNORECASE) is not None
    except re.error as exc:
        raise RuntimeError("IDENTITY_NUMBER_REGEX is not a valid regular expression.") from exc
    if not valid:
        raise ValueError("Enter a valid identity number.")
    return normalized


def _master_key() -> bytes:
    # Development has a deterministic, explicitly non-production fallback so a local
    # SQLite database remains readable between restarts. Production requires a separate key.
    value = settings.pii_master_key or (settings.session_secret + "|bizflow-development-pii-key")
    return value.encode("utf-8")


def _derived_key(label: bytes) -> bytes:
    return hmac.new(_master_key(), label, hashlib.sha256).digest()


def identity_lookup_hash(normalized: str) -> str:
    return hmac.new(_derived_key(b"bizflow:identity-lookup:v1"), normalized.encode("utf-8"), hashlib.sha256).hexdigest()


def identity_lookup_hash_from_search(value: Any) -> str | None:
    try:
        raw = clean_text(value, limit=80, label="Search").strip()
    except ValueError:
        return None
    normalized = re.sub(r"[\s-]+", "", raw).upper()
    return identity_lookup_hash(normalized) if normalized else None


def encrypt_identity_number(normalized: str) -> str:
    key = base64.urlsafe_b64encode(_derived_key(b"bizflow:identity-encryption:v1"))
    return Fernet(key).encrypt(normalized.encode("utf-8")).decode("ascii")


def decrypt_identity_number(ciphertext: str | None) -> str | None:
    if not ciphertext:
        return None
    key = base64.urlsafe_b64encode(_derived_key(b"bizflow:identity-encryption:v1"))
    try:
        return Fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError) as exc:
        raise RuntimeError("Identity-number decryption failed; verify the configured PII_MASTER_KEY.") from exc


def mask_identity_number(value: str | None) -> str:
    return "••••••••" + value[-4:] if value else "Not provided"


def generate_temporary_password(length: int = 16) -> str:
    if length < 12:
        raise ValueError("Temporary passwords must contain at least 12 characters.")
    rng = secrets.SystemRandom()
    alphabet = string.ascii_letters + string.digits
    chars = [rng.choice(string.ascii_lowercase), rng.choice(string.ascii_uppercase), rng.choice(string.digits)]
    chars.extend(rng.choice(alphabet) for _ in range(length - len(chars)))
    rng.shuffle(chars)
    return "".join(chars)


def _next_sequence(db: Any) -> int:
    db.execute("INSERT INTO account_sequences(name,next_value) VALUES('customer',10000) ON CONFLICT(name) DO NOTHING")
    query = "SELECT next_value FROM account_sequences WHERE name='customer'"
    if getattr(db, "dialect", "sqlite") == "postgres":
        query += " FOR UPDATE"
    row = db.execute(query).fetchone()
    number = int(row["next_value"]) + 1
    db.execute("UPDATE account_sequences SET next_value=? WHERE name='customer'", (number,))
    return number


def generate_account_identifiers(db: Any) -> tuple[str, str]:
    """Return a new unique (username, public account ID) inside the caller's transaction."""
    for _ in range(10000):
        number = _next_sequence(db)
        username = f"BIZ-{number:05d}"
        account_id = f"USR-{number:05d}"
        taken = db.execute(
            "SELECT id FROM users WHERE upper(COALESCE(username,''))=? OR upper(COALESCE(public_account_id,''))=? LIMIT 1",
            (username, account_id),
        ).fetchone()
        if not taken:
            return username, account_id
    raise RuntimeError("Unable to allocate a unique BizFlow account identifier.")


def backfill_account_identifiers(db: Any) -> None:
    """Assign stable references to prior users without inspecting or guessing identity data."""
    with transaction(db):
        db.execute("INSERT INTO account_sequences(name,next_value) VALUES('customer',10000) ON CONFLICT(name) DO NOTHING")
        rows = db.execute("SELECT id,username,public_account_id FROM users ORDER BY created_at,id").fetchall()
        counter = db.execute("SELECT next_value FROM account_sequences WHERE name='customer'").fetchone()
        highest = int(counter["next_value"])
        for row in rows:
            for value in (row["username"], row["public_account_id"]):
                match = re.fullmatch(r"(?:BIZ|USR)-(\d+)", str(value or ""), flags=re.IGNORECASE)
                if match:
                    highest = max(highest, int(match.group(1)))
        db.execute("UPDATE account_sequences SET next_value=? WHERE name='customer'", (highest,))

        seen_usernames: set[str] = set()
        seen_account_ids: set[str] = set()
        for row in rows:
            username = str(row["username"] or "").strip()
            account_id = str(row["public_account_id"] or "").strip()
            replace_username = not username or username.casefold() in seen_usernames
            replace_account_id = not account_id or account_id.casefold() in seen_account_ids
            while replace_username or replace_account_id:
                number = _next_sequence(db)
                candidate_username = f"BIZ-{number:05d}"
                candidate_account_id = f"USR-{number:05d}"
                if replace_username and db.execute(
                    "SELECT id FROM users WHERE upper(username)=? AND id<>? LIMIT 1", (candidate_username, row["id"])
                ).fetchone():
                    continue
                if replace_account_id and db.execute(
                    "SELECT id FROM users WHERE upper(public_account_id)=? AND id<>? LIMIT 1", (candidate_account_id, row["id"])
                ).fetchone():
                    continue
                if replace_username:
                    username = candidate_username
                if replace_account_id:
                    account_id = candidate_account_id
                break
            db.execute("UPDATE users SET username=?,public_account_id=? WHERE id=?", (username, account_id, row["id"]))
            seen_usernames.add(username.casefold())
            seen_account_ids.add(account_id.casefold())
