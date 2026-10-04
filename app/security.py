from __future__ import annotations

import hashlib
import hmac
import secrets
import re
from datetime import datetime, timezone
from typing import Any

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1,
                                  hash_len=32, salt_len=16, type=Type.ID)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_dt(value: str | datetime | None) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        out = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def make_id() -> str:
    return secrets.token_hex(16)


def new_secret() -> str:
    return secrets.token_urlsafe(32)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 10 or len(password) > 128:
        raise ValueError("Password must be between 10 and 128 characters.")
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, encoded: str | None) -> bool:
    if not isinstance(password, str) or not encoded or len(password) > 128:
        return False
    if encoded.startswith("$argon2id$"):
        try:
            return _PASSWORD_HASHER.verify(encoded, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
            return False
    # Backward-compatible verification for existing accounts. New and changed passwords
    # are always stored as Argon2id; successful legacy logins can be upgraded in-place.
    try:
        algo, n_text, r_text, p_text, salt_hex, key_hex = encoded.split("$", 5)
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(key_hex)
        n, r, p = int(n_text), int(r_text), int(p_text)
        if algo != "scrypt" or (n, r, p, len(salt), len(expected)) != (1 << 14, 8, 1, 16, 64):
            return False
        result = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected),
                                maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(result, expected)
    except (ValueError, TypeError, MemoryError):
        return False


def password_needs_rehash(encoded: str | None) -> bool:
    if not encoded or not encoded.startswith("$argon2id$"):
        return True
    try:
        return _PASSWORD_HASHER.check_needs_rehash(encoded)
    except (VerificationError, InvalidHashError, ValueError):
        return True


def clean_text(value: Any, *, limit: int = 500, required: bool = False, label: str = "Value") -> str:
    if value is None:
        value = ""
    text = str(value).strip()
    if len(text) > limit:
        raise ValueError(f"{label} must be {limit} characters or fewer.")
    if required and not text:
        raise ValueError(f"{label} is required.")
    return text


def clean_email(value: Any) -> str:
    email = clean_text(value, limit=254, required=True, label="Email Address").lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValueError("Enter a valid email address.")
    return email


def clean_phone(value: Any) -> str:
    phone = clean_text(value, limit=40, required=True, label="Phone Number")
    if not re.fullmatch(r"\+?[0-9() .-]{7,40}", phone) or not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        raise ValueError("Enter a valid phone number.")
    return phone


def clean_money(value: Any, *, label: str = "Amount", allow_zero: bool = True) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a valid amount.") from None
    if number < 0 or (not allow_zero and number <= 0) or number > 1_000_000_000 or round(number) != number:
        qualifier = "greater than zero" if not allow_zero else "a non-negative whole number"
        raise ValueError(f"{label} must be {qualifier}.")
    return int(number)


def clean_quantity(value: Any, *, label: str = "Quantity", allow_zero: bool = True) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a valid number.") from None
    if number < 0 or (not allow_zero and number <= 0) or number > 1_000_000 or not (number < float("inf")):
        qualifier = "greater than zero" if not allow_zero else "a non-negative number"
        raise ValueError(f"{label} must be {qualifier}.")
    return round(number, 3)


def sanitize_filename(name: str) -> str:
    base = name.replace("\\", "/").split("/")[-1]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._")
    return safe[:120] or "receipt"
