# BizFlow LK

BizFlow LK is a FastAPI business-management application with a responsive, dependency-free HTML/CSS/JavaScript frontend. The existing product includes sales/POS, inventory, customers, suppliers, invoices, quotations, expenses, business reports, staff roles, customer accounts, platform administration, manual bank-transfer billing, and subscription expiry handling.

**Release status: not production-ready until the external deployment checks in this guide are completed.** This workspace has no configured production database, public frontend/API origins, persistent volume, backup provider, Netlify project, or live deployment credentials. A green local test suite is not evidence of a live production release.

## Architecture

- **Backend:** FastAPI; the API and frontend are served together for a same-origin deployment.
- **Frontend:** static files in `web/`; API calls use relative `/api/...` URLs.
- **Database:** SQLite for local development and tests; PostgreSQL through psycopg for staging/production. Production startup rejects SQLite.
- **Uploads:** receipts, expense attachments, and product images are stored outside the source tree under `STORAGE_DIR`. These are private filesystem objects, not public static files.
- **Netlify:** `netlify.toml` invokes `scripts/build_netlify.py`, which copies the current `web/` app, creates the SPA fallback, and writes a same-origin `/api/*` proxy using the real `API_URL` configured in Netlify. No deployment host or domain is assumed by this repository.

## Local development

Python 3.10 or newer is required; Python 3.11 or newer is recommended.

### Windows

```powershell
.\run-local.ps1
```

The launcher creates a local virtual environment, installs runtime dependencies, then interactively creates a local `.env`, applies migrations, bootstraps the first administrator, and starts Uvicorn. The initial administrator password is entered privately, used only in the setup process to create a password hash, and is never written to `.env` or printed. The server binds to loopback unless `-Lan` is explicitly selected; LAN mode is for trusted local testing only, not public deployment.

### Linux/macOS

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
cp .env.example .env
python scripts/setup_local.py
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open the local URL printed by the launcher. Local development uses SQLite under `data/` and private uploads under `data/private/`. `.env`, databases, upload directories, build output, logs, and local backups are ignored by `.gitignore`; do not add them to a commit or archive. `.env.example` contains variable names only, with blank values.

To run checks:

```bash
python -m compileall -q app scripts tests
node --check web/assets/public.js
node --check web/assets/app.js
node --check web/assets/pages.js
node --check web/assets/admin_views.js
node --check web/assets/core.js
pytest -q
```

Install `requirements-dev.txt` to get pytest and httpx2 for Starlette's test client. Production installs should use `requirements.txt` only.

## Configuration

### Required for a staging/production API

Set these in the backend host's private environment/secret manager; do not commit or publish their values:

| Variable | Purpose |
|---|---|
| `APP_ENV` | Set to `staging` or `production`; production must be exactly `production`. |
| `DATABASE_URL` | A reachable PostgreSQL URL (`postgresql://...`). Use the provider's TLS-capable connection settings. |
| `SESSION_SECRET` | A random secret of at least 32 characters for CSRF-token derivation. Keep it stable; changing it invalidates existing sessions. |
| `PII_MASTER_KEY` | A separate random secret of at least 32 characters used to encrypt identity numbers and derive their lookup hashes. Back it up securely; do not rotate it without a planned data re-encryption. |
| `STORAGE_DIR` | Absolute path to an existing, writable directory on a persistent mounted volume, outside the application source directory. Every API replica must see the same storage if replicas are used. |
| `FRONTEND_URL` | The real public HTTPS frontend origin, without a path. It is used for canonical/sitemap URLs and the exact CORS allow-origin. |

The application validates these settings during startup. Production also requires applied migrations, a provisioned Super Admin, and a writable persistent storage path. It does not create/alter the production schema or seed accounts when the web process starts.

### One-time initial administrator bootstrap

For a new database with no Super Admin, set `ADMIN_EMAIL` and `ADMIN_PASSWORD` **only in the protected environment of the one-off migration/bootstrap command**. The password must be 14–128 characters. Run `python scripts/migrate.py`; after the account is created, remove `ADMIN_PASSWORD` (and `ADMIN_EMAIL` if no longer needed) from that command's environment. If a Super Admin already exists, the seed step does not change its password. Password recovery remains administrator-assisted: a temporary password is returned once to an authorized Super Admin, stored only as a password hash, and requires a change at next sign-in.

### Netlify frontend build variables

Configure these in the Netlify build environment, not as client-side secrets:

| Variable | Purpose |
|---|---|
| `FRONTEND_URL` | The actual HTTPS origin where this Netlify site will be served. Set the same origin in the backend configuration. |
| `API_URL` | The actual HTTPS origin of the FastAPI service, with no path. The build writes it into the public Netlify proxy rule; it is not a credential. |

The generated `_redirects` file forwards `/api/*` to `${API_URL}/api/:splat` and then falls back to `/index.html`. The browser continues to call same-origin `/api/...` paths. The backend's CORS policy permits only its configured `FRONTEND_URL`; it does not enable wildcard origins or wildcard credentialed access.

Do not build or deploy until the real origins are known. No Netlify/API domain is supplied or guessed in this repository.

### Optional settings and legacy compatibility

- `PORT`: platform-provided web port; local default is `8000`.
- `MAX_RECEIPT_MB`: upload-size cap, default `5` (bounded by the application to 1–15 MB).
- `IDENTITY_NUMBER_REGEX`: identity-number format validation pattern; a Sri Lankan NIC pattern is used if omitted.
- `JWT_SECRET`: legacy development-only alias for `SESSION_SECRET`. Move deployments to `SESSION_SECRET`; staging/production reject the alias.

## Database migrations and application start

Migrations are numbered Python modules in `database/migrations/`; `schema_migrations` records each applied version. `database/schema.sqlite.sql` and `database/schema.postgres.sql` describe the baseline schemas. Do not edit an already-applied migration to change a live database—add a new numbered migration and test both SQLite and PostgreSQL behavior.

Before each deployment:

1. Take a verified backup of the database and private storage.
2. Run `python scripts/migrate.py` once as a deployment/one-off task with the target `DATABASE_URL` and protected secrets. The migration preflight refuses to add the single-pending-payment constraint if multiple pending requests exist for a business; review and resolve them before retrying.
3. Confirm the migration command exits successfully and the Super Admin exists.
4. Start the API only after migrations complete. Production startup performs a read-only migration check and fails closed when versions are missing.

A typical managed-process command is `uvicorn app.main:app --host 0.0.0.0 --port $PORT`; configure the host's trusted proxy/forwarded-header settings according to that provider rather than trusting arbitrary forwarded headers. HTTPS termination, the public service origin, process supervision, and health-check path must be configured by the selected host. `/api/health` checks database connectivity and, in a deployment environment, migration state. API documentation and OpenAPI are disabled in staging/production.

## Subscription expiry and scheduled tasks

The long-running API has an hourly background billing task. With PostgreSQL, a session advisory lock elects one live process as scheduler leader so ordinary multi-worker deployments do not run a duplicate scheduler. Business access also checks subscription expiry during authenticated requests. For a host that sleeps, scales to zero, or does not guarantee a continuously running process, configure an hourly platform scheduler to run:

```bash
python scripts/expire_subscriptions.py
```

That command requires the same production configuration and a migrated database. The job is idempotent, records expiry/warning events, and does not delete business records. A real staging test must confirm that an expired subscription changes effective access while retaining historical data.

## Private uploads

Only JPG/JPEG, PNG, and PDF receipts are accepted where applicable. Upload endpoints enforce byte limits, match filename extension/MIME/signature, decode raster images, check PDF markers, and save files with owner-only file permissions in private subdirectories. Receipt and attachment routes require the customer/business context or Super Admin authorization and return private, non-cacheable responses. `STORAGE_DIR` must be a durable volume; the application cannot make an ephemeral filesystem durable or configure a cloud storage provider that has not been selected.

Keep the configured volume mounted at the same stable path. Restrict its ACLs and enable provider-level encryption at rest. If the service uses multiple hosts, use a shared durable volume or adopt a shared object-storage adapter before scaling; independent local disks are not sufficient. Include uploads in backup and restore tests alongside PostgreSQL.

## Backup and recovery policy

No backup provider, schedule, retention, or recovery procedure is configured in this workspace. Before launch, the operator must select and verify one. A reasonable initial policy is automated daily encrypted PostgreSQL backups, at least 30 days of retention, separate backups of `STORAGE_DIR`, restricted access to both secrets and backups, and a documented restore test at least quarterly. Choose retention based on business and legal requirements rather than treating that recommendation as an existing service guarantee.

Example PostgreSQL backup/inspection commands (run in a protected shell with the real `DATABASE_URL`; do not paste credentials into tickets or logs):

```bash
pg_dump --format=custom "$DATABASE_URL" --file="bizflow-backup.dump"
pg_restore --list "bizflow-backup.dump"
```

A restore test must use an isolated database and a matching private-storage snapshot and must verify login, identity-number decryption with the backed-up `PII_MASTER_KEY`, receipt access, tenant boundaries, and subscription history. Loss of `PII_MASTER_KEY` makes encrypted identity values unreadable. Database-only backups are incomplete if uploaded receipts and product/expense attachments are not included.

## Security and privacy notes

- Passwords are newly stored as Argon2id hashes; existing scrypt hashes are accepted and upgraded after successful sign-in. Hashes are not returned by public APIs.
- Sessions are opaque, server-side, HttpOnly cookies with CSRF checks. Password reset revokes existing sessions; suspension now revokes the suspended user's sessions as well.
- Identity numbers are encrypted at rest and indexed by a keyed hash; ordinary account responses and admin search lists mask them. Only the authorized Super Admin identity-detail route returns a full value, and that access is audited. Identity values are not placed in URLs or support-message templates.
- Login, registration, password-change, payment-submission, and administrator-reset attempts use database-backed rate limits. Configure the hosting edge/proxy's own abuse controls as an additional layer and make sure trusted-proxy client IP handling is correct.
- Manual bank transfer remains the only payment method for platform subscriptions. Uploading a receipt never activates a paid plan. An administrator must explicitly confirm the bank credit. Approved paid terms default to 30 days; early renewals extend the existing expiry.
- The privacy notice and terms page are product copy, not a claim of legal compliance or legal advice. Have them reviewed for the actual operating entity, jurisdiction, contact channels, retention practice, and provider contracts before public launch. Configure real support contact details in the Super Admin settings.

## Release checklist — still required outside this workspace

- [ ] Configure the real public frontend origin, API origin/proxy, HTTPS, and trusted proxy headers.
- [ ] Provision managed PostgreSQL and run migrations against a staging copy of real schema/data.
- [ ] Provision a persistent/private upload volume; test permissions, cross-replica visibility if applicable, backup, and restore.
- [ ] Configure and verify the backup schedule, retention, encryption, and restore owner.
- [ ] Run the expiry scheduler on the actual host and observe one staged expiration/renewal cycle.
- [ ] Complete staging checks for registration/login, temporary-password reset/change, tenant isolation, Super Admin identity view/audit, receipt privacy, manual bank approval/rejection, 30-day renewal/expiry, mobile layout, and keyboard/screen-reader accessibility.
- [ ] Configure real support contacts, bank details, and legal-reviewed notices; verify actual payment and support workflows with authorized operators.
- [ ] Inspect the actual Git repository/history and rotate any credentials that were ever committed. This workspace is not itself a Git checkout, so GitHub history, remotes, secrets, and CI settings cannot be verified here.
- [ ] Perform and document the actual Netlify/backend deployment smoke test. No live production connection or deployment was available during this engineering pass.
