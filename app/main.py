from __future__ import annotations

import asyncio
import html
import hmac
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth_routes import router as auth_router
from app.admin_routes import router as admin_router
from app.billing_routes import router as billing_router
from app.business_base import router as business_base_router
from app.business_reports import router as reports_router
from app.business_sales import router as sales_router
from app.config import ROOT, settings, validate_runtime_settings
from app.db import (apply_migrations, assert_migrations_current, connect, is_postgres)
from app.deps import SESSION_COOKIE
from app.security import digest_secret, iso_utc, parse_dt, utc_now
from app.services import run_billing_jobs, seed_database

logging.basicConfig(level=logging.INFO if not settings.is_production else logging.WARNING,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("bizflow")
web_root = ROOT / "web"
assets_dir = web_root / "assets"
INDEXABLE_PATHS = {"/", "/features", "/pricing", "/faq", "/contact", "/privacy", "/terms"}
BILLING_SCHEDULER_LOCK = 7_314_2026
MAX_REQUEST_BYTES = max(4, settings.max_receipt_mb + 2) * 1024 * 1024


def _secure_headers(response: Response, request: Request) -> None:
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    if request.url.path.startswith("/api/"):
        # Netlify and other reverse proxies must not cache private API responses.
        response.headers.setdefault("Cache-Control", "no-store")
    frame_ancestors = ("frame-ancestors 'self'" if settings.is_deployment else
                       "frame-ancestors 'self' https://arena.ai https://*.arena.ai https://*.e2b.app")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; "
        "connect-src 'self'; font-src 'self' data:; object-src 'none'; base-uri 'self'; "
        f"form-action 'self'; {frame_ancestors}",
    )
    if settings.is_deployment:
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if request.url.scheme == "https" or forwarded == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        response.headers.setdefault("X-Request-ID", request_id)


class RequestSizeLimitMiddleware:
    """Hard cap API bodies, including chunked requests without Content-Length."""

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not scope.get("path", "").startswith("/api/"):
            await self.app(scope, receive, send)
            return
        raw_length = next((value for key, value in scope.get("headers", []) if key.lower() == b"content-length"), b"")
        try:
            declared_length = int(raw_length) if raw_length else 0
        except ValueError:
            declared_length = 0
        if declared_length > self.max_bytes:
            body = b'{"detail":"Request body is too large."}'
            headers = [(b"content-type", b"application/json; charset=utf-8"),
                       (b"content-length", str(len(body)).encode("ascii")),
                       (b"cache-control", b"no-store")]
            await send({"type": "http.response.start", "status": 413, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        total = 0

        async def limited_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            body = b'{"detail":"Request body is too large."}'
            headers = [(b"content-type", b"application/json; charset=utf-8"),
                       (b"content-length", str(len(body)).encode("ascii")),
                       (b"cache-control", b"no-store")]
            await send({"type": "http.response.start", "status": 413, "headers": headers})
            await send({"type": "http.response.body", "body": body, "more_body": False})


class _RequestBodyTooLarge(Exception):
    pass


async def _scheduled_billing_job() -> None:
    # A PostgreSQL session advisory lock elects one live application process as the
    # scheduler leader. Losing the connection releases the lock automatically.
    db = None
    owns_lock = False
    while True:
        try:
            if db is None:
                db = connect()
                if is_postgres():
                    result = db.execute("SELECT pg_try_advisory_lock(?) AS locked", (BILLING_SCHEDULER_LOCK,)).fetchone()
                    owns_lock = bool(result and result["locked"])
                    if not owns_lock:
                        db.close()
                        db = None
                        await asyncio.sleep(60)
                        continue
            await asyncio.to_thread(run_billing_jobs, db)
        except asyncio.CancelledError:
            if db is not None:
                db.close()
            raise
        except Exception as exc:
            # Never log the exception text or traceback: database errors can contain
            # customer-supplied values. The type is enough to correlate a safe alert.
            logger.error("Scheduled subscription job failed error_type=%s", type(exc).__name__)
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass
            db = None
            owns_lock = False
            await asyncio.sleep(60)
            continue
        await asyncio.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    validate_runtime_settings(settings)
    if settings.is_deployment:
        assert_migrations_current()
        storage_path = settings.storage_dir.resolve()
        if not storage_path.is_dir() or not os.access(storage_path, os.W_OK | os.X_OK):
            raise RuntimeError("STORAGE_DIR must exist and be writable on the persistent volume before the API starts.")
        db = connect()
        try:
            admin = db.execute("SELECT id FROM users WHERE role='SUPER_ADMIN' LIMIT 1").fetchone()
            if not admin:
                raise RuntimeError("No Super Admin account exists. Run the migration/bootstrap command with ADMIN_EMAIL and ADMIN_PASSWORD before starting the API.")
            run_billing_jobs(db)
        finally:
            db.close()
    else:
        # Local development and isolated tests may initialize an ephemeral schema.
        apply_migrations()
        db = connect()
        try:
            seed_database(db, settings)
            run_billing_jobs(db)
        finally:
            db.close()
    app.state.billing_task = None
    if settings.app_env != "test":
        app.state.billing_task = asyncio.create_task(_scheduled_billing_job())
    yield
    task = app.state.billing_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="BizFlow LK",
    version="1.0.0",
    description="Business management with administrator-approved, 30-day bank-transfer subscriptions.",
    docs_url=None if settings.is_deployment else "/api/docs",
    redoc_url=None,
    openapi_url=None if settings.is_deployment else "/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(RequestSizeLimitMiddleware, max_bytes=MAX_REQUEST_BYTES)
if settings.frontend_url:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_url],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token"],
        expose_headers=["X-Request-ID", "Retry-After"],
        max_age=600,
    )

app.include_router(auth_router)
app.include_router(business_base_router)
app.include_router(sales_router)
app.include_router(reports_router)
app.include_router(billing_router)
app.include_router(admin_router)


@app.middleware("http")
async def security_and_csrf(request: Request, call_next):
    request.state.request_id = uuid.uuid4().hex
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        # SameSite cookies are the first layer. This synchronizer token protects every
        # authenticated mutation; JSON/custom headers prevent form-based CSRF on auth routes.
        fetch_site = request.headers.get("sec-fetch-site", "").lower()
        if fetch_site == "cross-site":
            response = JSONResponse({"detail": "Cross-site requests are not allowed."}, status_code=403)
            _secure_headers(response, request)
            return response
        raw = request.cookies.get(SESSION_COOKIE)
        if raw:
            db = connect()
            try:
                row = db.execute("SELECT csrf_hash,expires_at FROM sessions WHERE token_hash=?", (digest_secret(raw),)).fetchone()
            finally:
                db.close()
            if row and parse_dt(row["expires_at"]) and parse_dt(row["expires_at"]) > utc_now():
                provided = request.headers.get("x-csrf-token", "")
                actual = digest_secret(provided) if provided else ""
                if not hmac.compare_digest(row["csrf_hash"], actual):
                    response = JSONResponse({"detail": "Security token missing or expired. Refresh the page and try again."}, status_code=403)
                    _secure_headers(response, request)
                    return response
    response = await call_next(request)
    _secure_headers(response, request)
    return response


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


@app.exception_handler(RequestValidationError)
async def request_validation_handler(request: Request, exc: RequestValidationError):
    # Do not return framework internals to the browser.
    return JSONResponse({"detail": "Please check the information and try again."}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception):
    logger.error("Unhandled request error request_id=%s method=%s path=%s error_type=%s",
                 getattr(request.state, "request_id", "unknown"), request.method, request.url.path,
                 type(exc).__name__)
    return JSONResponse({"detail": "We couldn't complete that request. Please try again."}, status_code=500)


@app.get("/api/health", tags=["system"])
def health():
    db = connect()
    try:
        if settings.is_deployment:
            assert_migrations_current(db)
        db.execute("SELECT 1").fetchone()
    finally:
        db.close()
    return {"status": "ok", "service": "BizFlow LK"}


@app.get("/api/public/settings", tags=["public"])
def public_settings():
    import json
    db = connect()
    try:
        row = db.execute("SELECT value FROM app_settings WHERE key='general'").fetchone()
        values = json.loads(row["value"]) if row else {}
        support_phone = values.get("support_phone", "")
        support_whatsapp = values.get("support_whatsapp") or values.get("whatsapp_number", "")
        return {
            "website_name": values.get("website_name", "BizFlow LK"),
            "business_name": values.get("business_name", "BizFlow LK"),
            "support_email": values.get("support_email", ""),
            "support_phone": support_phone,
            "support_whatsapp": support_whatsapp,
            "whatsapp_number": support_whatsapp,
            "support_message": values.get("support_message", "Contact the configured BizFlow support channel for account help."),
        }
    finally:
        db.close()


@app.get("/robots.txt", include_in_schema=False)
def robots(request: Request):
    base = settings.frontend_url or str(request.base_url).rstrip("/")
    content = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /admin\n"
        "Disallow: /dashboard\n"
        "Disallow: /account\n"
        "Disallow: /billing\n"
        "Disallow: /products\n"
        "Disallow: /inventory\n"
        "Disallow: /sales\n"
        "Disallow: /customers\n"
        "Disallow: /suppliers\n"
        "Disallow: /invoices\n"
        "Disallow: /quotations\n"
        "Disallow: /expenses\n"
        "Disallow: /reports\n"
        "Disallow: /settings\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return PlainTextResponse(content, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap(request: Request):
    base = settings.frontend_url or str(request.base_url).rstrip("/")
    paths = ("/", "/features", "/pricing", "/faq", "/contact", "/privacy", "/terms")
    entries = "".join(f"  <url><loc>{html.escape(base + path)}</loc></url>\n" for path in paths)
    body = '<?xml version="1.0" encoding="UTF-8"?>\n' + (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + entries + '</urlset>\n'
    )
    return Response(content=body, media_type="application/xml")


app.mount("/assets", StaticFiles(directory=str(assets_dir), check_dir=False), name="assets")


def _index_response(path: str = "") -> HTMLResponse:
    source = (web_root / "index.html").read_text(encoding="utf-8")
    normalized = "/" + path.strip("/") if path else "/"
    indexable = normalized in INDEXABLE_PATHS
    base = settings.frontend_url.rstrip("/") if settings.frontend_url else ""
    canonical = html.escape(base + normalized, quote=True) if base else ""
    seo = ""
    if canonical:
        seo = (f'<link rel="canonical" href="{canonical}">'
               f'<meta property="og:url" content="{canonical}">'
               f'<meta property="og:image" content="{html.escape(base + "/assets/bizflow-logo.png", quote=True)}">')
    if not indexable:
        seo += '<meta name="robots" content="noindex,nofollow">'
    source = source.replace("<!--SEO_META-->", seo)
    headers = {"X-Robots-Tag": "index, follow" if indexable else "noindex, nofollow",
               "Cache-Control": "no-store"}
    return HTMLResponse(source, headers=headers)


@app.get("/", include_in_schema=False)
def index():
    return _index_response("/")


@app.get("/{path:path}", include_in_schema=False)
def spa_fallback(path: str):
    if path.startswith("api/") or path.startswith("assets/") or path in {"favicon.ico", "robots.txt", "sitemap.xml"}:
        raise StarletteHTTPException(status_code=404, detail="Not found.")
    return _index_response(path)
