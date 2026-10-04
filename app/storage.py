from __future__ import annotations

import io
import os
import secrets
from pathlib import Path

from PIL import Image, UnidentifiedImageError


def verify_raster_image(content: bytes, extension: str) -> bool:
    expected_format = "PNG" if extension.lower() == ".png" else "JPEG" if extension.lower() in {".jpg", ".jpeg"} else ""
    if not expected_format:
        return False
    try:
        with Image.open(io.BytesIO(content)) as image:
            if image.format != expected_format or image.width * image.height > 25_000_000:
                return False
            image.verify()
        return True
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return False


def ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.chmod(0o700)
    except OSError:
        # Windows filesystems may not implement POSIX permission bits; deployment
        # volumes must still be protected by their platform ACLs.
        pass


def write_private_file(path: Path, content: bytes) -> None:
    """Durably write an uploaded file with owner-only mode and an atomic final rename."""
    ensure_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.part")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    fd = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        temporary.unlink(missing_ok=True)
        raise
