#!/usr/bin/env python3
"""Build the existing static frontend with a same-origin Netlify API proxy."""
from __future__ import annotations

import html
import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
OUTPUT = ROOT / "dist"
PUBLIC_PATHS = ("/", "/features", "/pricing", "/faq", "/contact", "/privacy", "/terms")
PRIVATE_PATHS = (
    "/login", "/register", "/forgot-password", "/change-password", "/dashboard", "/account",
    "/admin", "/admin/*", "/products", "/products/*", "/inventory", "/inventory/*",
    "/sales", "/sales/*", "/customers", "/customers/*", "/suppliers", "/suppliers/*",
    "/invoices", "/invoices/*", "/quotations", "/quotations/*", "/expenses", "/expenses/*",
    "/reports", "/reports/*", "/staff", "/staff/*", "/billing", "/billing/*",
    "/settings", "/settings/*",
)


def _origin(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not host or parsed.username is not None or parsed.password is not None
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
                or host in {"localhost", "127.0.0.1", "0.0.0.0"}):
            raise ValueError
        return f"https://{parsed.netloc}"
    except (ValueError, AttributeError):
        raise SystemExit(f"{name} must be a real HTTPS origin (scheme and host only). Set it in the Netlify build environment.") from None


def _robots(frontend: str) -> str:
    private = ("/api/", "/admin", "/dashboard", "/account", "/billing", "/products", "/inventory",
               "/sales", "/customers", "/suppliers", "/invoices", "/quotations", "/expenses",
               "/reports", "/staff", "/settings")
    lines = ["User-agent: *", *(f"Disallow: {path}" for path in private), f"Sitemap: {frontend}/sitemap.xml", ""]
    return "\n".join(lines)


def _sitemap(frontend: str) -> str:
    entries = "\n".join(f"  <url><loc>{html.escape(frontend + path)}</loc></url>" for path in PUBLIC_PATHS)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n' + entries + '\n</urlset>\n'
    )


def build(frontend_value: str, api_value: str) -> Path:
    frontend = _origin(frontend_value, "FRONTEND_URL")
    api = _origin(api_value, "API_URL")
    if OUTPUT.exists():
        shutil.rmtree(OUTPUT)
    shutil.copytree(WEB, OUTPUT)

    index_path = OUTPUT / "index.html"
    index = index_path.read_text(encoding="utf-8")
    canonical = html.escape(frontend + "/", quote=True)
    image = html.escape(frontend + "/assets/bizflow-logo.png", quote=True)
    seo = (f'<link rel="canonical" href="{canonical}">'
           f'<meta property="og:url" content="{canonical}">'
           f'<meta property="og:image" content="{image}">'
           '<meta name="robots" content="index,follow">')
    index_path.write_text(index.replace("<!--SEO_META-->", seo), encoding="utf-8")

    redirects = f"/api/* {api}/api/:splat 200\n/* /index.html 200\n"
    (OUTPUT / "_redirects").write_text(redirects, encoding="utf-8")
    (OUTPUT / "robots.txt").write_text(_robots(frontend), encoding="utf-8")
    (OUTPUT / "sitemap.xml").write_text(_sitemap(frontend), encoding="utf-8")

    csp = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
           "img-src 'self' data: blob:; connect-src 'self'; font-src 'self' data:; "
           "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
    headers = [
        "/*", "  X-Content-Type-Options: nosniff",
        "  Referrer-Policy: strict-origin-when-cross-origin",
        "  Permissions-Policy: camera=(), microphone=(), geolocation=()",
        "  X-Frame-Options: SAMEORIGIN",
        "  Strict-Transport-Security: max-age=31536000",
        f"  Content-Security-Policy: {csp}",
    ]
    for path in PRIVATE_PATHS:
        headers.extend(["", path, "  X-Robots-Tag: noindex, nofollow"])
    (OUTPUT / "_headers").write_text("\n".join(headers) + "\n", encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    output = build(os.getenv("FRONTEND_URL", ""), os.getenv("API_URL", ""))
    print(f"Netlify frontend built at {output}")
