# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Quickly is a self-hosted cold email platform (an open-source Instantly clone): FastAPI + async SQLAlchemy backend in `app/`, React 18 + Vite + Tailwind SPA in `frontend/`, PostgreSQL, APScheduler running in-process. Emails go out via the Gmail API, Microsoft Graph (both OAuth2), or SMTP.

## Commands

Backend (Python 3.12):
```bash
pip install -r requirements.txt
cp .env.example .env              # set BASE_URL=http://localhost:8000
uvicorn app.main:app --reload     # API on :8000
```
It needs a Postgres `DATABASE_URL` (`postgresql+asyncpg://...`; plain `postgres://` / `postgresql://` are rewritten automatically). `QUICKLY_MODE=development` selects dev mode.

Frontend:
```bash
cd frontend && npm install && npm run dev   # :5173, proxies /api and /oauth to :8000 (override with VITE_API_URL)
cd frontend && npm run build
```
The frontend has no linter or test runner.

Tests (pytest, `asyncio_mode = auto`):
```bash
pytest                                        # defaults to in-memory SQLite, no Postgres needed
pytest tests/test_queue_logic.py              # one file
pytest tests/test_queue_logic.py::test_name   # one test
TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@localhost/test_quickly pytest   # against Postgres (tables are created/dropped — use a throwaway DB)
```
If `TEST_DATABASE_URL` points at an unreachable Postgres, `tests/conftest.py` silently falls back to SQLite. It also provides factory helpers for Campaign/Inbox/Sequence/Lead/QueueSlot etc.

Docker: `docker compose -f docker-compose.dev.yml up` (Postgres on host port 5433, backend on :8000 with reload, frontend dev server). `docker-compose.yml` is production (Postgres + app + Caddy); the `no-caddy` / `not-host` variants cover the other deployment layouts described in `docs/INSTALL.md`.

n8n node: `cd n8n-node && npm run build` (tsc).

## Architecture

**Startup (`app/main.py` lifespan):** `init_db()` runs `create_all` and then `_run_migrations` in `app/database.py`. That function is the only migration system (there is no Alembic). New columns on existing tables must be added there as idempotent `ALTER TABLE ... IF NOT EXISTS` statements. They are Postgres-only and skipped on SQLite. One-time backfills are gated through the `_app_schema_migrations` table. The lifespan then starts APScheduler with a Postgres job store (memory for SQLite) and registers these jobs: unibox sync (cron), Office 365 Graph subscription renewal, `slot_scan` every minute, and scheduled backups. It also kicks off a global queue recalculation and an inbox sync.

**Scheduling model, the core of the app:**
- `app/queue_logic.py` reserves a `QueueSlot` up front for every sequence step of every lead when leads are enrolled. Slots are assigned to inboxes by capacity (priority-first or round-robin strategy), per-inbox daily limits and ramp-up/warm-up, campaign sending days/hours/timezone, jitter, and lead email-provider matching (DNS). Changing wait days, sequences, or campaign inboxes triggers a recalculation (`recalculate_*`, `run_recalculate_all_in_new_session` in `app/routers/schedule.py`).
- `app/jobs.py`: `run_slot_scan_job` looks 60 s ahead each minute and spawns one asyncio task per due slot. `_pending_slot_ids` dedupes them. Each task sleeps until the slot's exact time and then calls `send_slot_job`, which handles send-window checks, bounce and auth failures (with a process-global per-inbox auth cooldown), enrollment status updates, and webhooks.
- `app/sender.py`: `send_email()` dispatches to `_send_via_gmail` / `_send_via_office365` / `_send_via_smtp`, renders `{{variables}}`, builds MIME, and handles reply threading and quoting.

**Other subsystems:**
- `app/unibox.py`: reply sync across all inboxes (Gmail, Graph, IMAP), threading, and AI reply classification via `app/ai_classifier.py` (`any-llm-sdk`, many providers).
- `app/tracking.py` + `routers/tracking.py` + `beacon_*`: open pixel, click redirects, and unsubscribe links. "Quickly Beacon" is an external tracking host that syncs events back through `routers/beacon_ingest.py`.
- `app/webhooks.py`: outbound webhooks (`fire_webhook_event`). Event types are documented in `docs/WEBHOOKS.md`.
- `app/mcp_leads.py`: FastMCP server mounted at `/api/mcp`, with its routes inserted before the SPA catch-all.
- `backup_*.py`: pg_dump/pg_restore backup, scheduled delivery, and restore staging.

**Auth:** JWT (access + refresh) or API keys, both in `app/auth.py`. Protected routers get `dependencies=[Depends(get_current_user)]` in `main.py`. OAuth callbacks, tracking, the Office 365 webhook, and the beacon ingest endpoint are public, and their routers enforce auth per endpoint. Registration is open only until the first (admin) user exists. Stored credentials and tokens are Fernet-encrypted through `app/security.py`, which also holds the SSRF guards for user-supplied URLs (webhooks, etc.). `docs/CONTRIBUTORS.md` still says there is no authentication, which is outdated.

**Settings:** `app/settings_manager.py` loads `.env` and then the DB `app_settings` table. Most runtime config (AI providers, sending windows, tracking domains) is set in the UI and stored in the DB. OAuth client IDs/secrets come only from env. `BASE_URL` from env always overrides the DB value. Read typed settings through the helpers in `app/app_settings.py`.

**Time:** use `from app import time as time_provider` (`now()`, `utcnow()`, `today()`) rather than `datetime.now()`. It applies the configurable `time_offset_days` used for time-travel testing. Datetimes are stored as naive UTC.

**Frontend:** pages are in `frontend/src/pages`, and all HTTP goes through `frontend/src/api.js`. Global state uses React Context providers (`src/context`) with no Redux. In production the backend serves the built SPA through a catch-all route in `main.py`. `QUICKLY_PREBUILT_IMAGE=1` marks the Docker-image layout (tests default it to `0`).

**Dev utilities:** `smoke_test/` holds data population and queue simulation scripts, and it ships in the image because a validation endpoint uses it. `scripts/` holds one-off maintenance and index scripts.
