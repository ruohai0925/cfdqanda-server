# cfdqanda-server

> **License:** [PolyForm Strict 1.0.0](https://polyformproject.org/licenses/strict/1.0.0/) — source available for personal and non-commercial use only.

Platform server layer for [CFDQandA](https://cfdqanda.com) — a natural-language-driven CFD simulation platform.

Contains the API server (FastAPI) and background worker that bridge the React frontend with the Foam-Agent simulation engine.

## Architecture

```
Browser (React)  ──►  API Server (FastAPI, port 8000)  ──►  Supabase (PostgreSQL + Storage + Realtime + Auth)
                                                                     ▲
                                                                     │
                                                              Worker (polling loop)
                                                                     │
                                                                     ▼
                                                              Foam-Agent subprocess
                                                              (LangGraph → OpenFOAM)
```

Two execution modes:

- **Auto mode** (`pipeline_mode='auto'`): Foam-Agent runs as a single subprocess to completion
- **Controlled mode** (`pipeline_mode='controlled'`): Stage-by-stage MCP pipeline with user-reviewable checkpoints

## Files

| File | Description |
|------|-------------|
| `api_server.py` | FastAPI application — 13 REST endpoints for task management, file browsing, feedback, user accounts, and storage |
| `worker.py` | Background worker — polls Supabase for jobs, launches Foam-Agent, uploads results (individual files + ZIP), manages data lifecycle |
| `mcp_client.py` | MCP client for controlled pipeline mode (plan, input_writer, run, review, apply_fixes, visualization) |
| `allrun_validator.py` | Security audit for OpenFOAM Allrun scripts (whitelist-based command validation) |
| `token_extractor.py` | Extracts LLM token usage statistics from simulation logs |
| `analyze_cases.py` | Analyze simulation tasks: status, model, error classification, per-user stats (`--md`, `--csv`, `--ssh`) |
| `check_storage.py` | Platform resource report: server disk/memory/Docker + Supabase storage usage (`--ssh`, `--quick`, `--detail`) |
| `cleanup_runs.sh` | Manual disk cleanup script for local `runs/` directories (supports `--all`, `--id`, `--range`, `--before`, `--largest N`, `--dry-run`) |
| `docker_start_workers.sh` | Start multiple Worker containers with separate WORKER_IDs, MCP ports, and log files |
| `docker_submit_test_tasks.sh` | Batch-submit test tasks via Supabase Auth login (for multi-worker testing) |
| `.env.example` | Environment variables template |
| `tests/` | 13 test files covering auth, security, concurrency, pipeline, etc. |

## Prerequisites

- Docker Engine installed (tested on WSL2 and GCP)
- Foam-Agent repository cloned locally
- Supabase project with `simulations` table, `user_profiles` table, `simulation_results` storage bucket, and `claim_next_job` RPC function

## Setup

```bash
cp .env.example .env
# Fill in all values, especially FOAM_AGENT_DIR
```

## Running

### Docker Compose (recommended)

```bash
# Build images (first time only, or after code changes)
docker build -f Dockerfile.api -t cfdqanda-api .
docker build -f Dockerfile.worker -t cfdqanda-worker .

# Start both services
docker compose up -d

# View logs
docker compose logs -f api
docker compose logs -f worker

# Check status
docker compose ps

# Stop
docker compose down
```

### Multi-Worker (concurrency testing)

```bash
docker compose up -d --scale worker=3
# Or use the helper script:
./docker_start_workers.sh 3
```

### Disk Cleanup

```bash
./cleanup_runs.sh --dry-run --largest 10   # Preview 10 largest runs
./cleanup_runs.sh --before 100             # Delete all runs with ID < 100
./cleanup_runs.sh --all                    # Delete all runs
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Health check |
| `POST` | `/api/v1/simulations` | JWT | Create a new simulation task |
| `GET` | `/api/v1/simulations/{id}/files` | JWT | Get file tree for a job |
| `POST` | `/api/v1/simulations/{id}/feedback` | JWT | Submit feedback on a specific file |
| `PATCH` | `/api/v1/simulations/{id}/rating` | JWT | Submit task-level or per-stage rating (1-3) |
| `DELETE` | `/api/v1/simulations/{id}` | JWT | Soft-delete a task |
| `POST` | `/api/v1/simulations/{id}/cancel` | JWT | Cancel a queued/running/checkpoint task |
| `POST` | `/api/v1/simulations/{id}/stage/confirm` | JWT | Confirm a pipeline checkpoint |
| `POST` | `/api/v1/simulations/{id}/stage/reject` | JWT | Reject a pipeline checkpoint |
| `POST` | `/api/v1/simulations/{id}/restore` | JWT | Restore a soft-deleted task |
| `GET` | `/api/v1/user/storage` | JWT | Get user's cloud storage usage summary |
| `POST` | `/api/v1/users/me/profile` | JWT | Create user profile (RLS fallback) |
| `DELETE` | `/api/v1/users/me` | JWT | Delete account and all associated data (GDPR) |
| `GET` | `/api/v1/admin/status` | — | Aggregated health: API + Worker status (for UptimeRobot) |

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FOAM_AGENT_DIR` | Yes | — | Absolute path to the Foam-Agent repository |
| `SUPABASE_URL` | Yes | — | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Yes | — | Supabase service_role key (bypasses RLS) |
| `SUPABASE_JWT_SECRET` | Yes | — | Supabase JWT secret for token verification |
| `OPENAI_API_KEY` | No | — | Default OpenAI API key (used for embeddings) |
| `WM_PROJECT_DIR` | No | `/opt/openfoam10` | OpenFOAM installation path |
| `EXTRA_CORS_ORIGINS` | No | — | Additional CORS origins (comma-separated) |
| `MIDDLEWARE_DIR` | No | — | Path to cfdqanda-middleware for checkpoint modules |
| `SIMULATION_TIMEOUT` | No | `3600` | Subprocess timeout in seconds |
| `STALE_JOB_THRESHOLD` | No | `7200` | Seconds before a running job is considered stale |
| `MCP_SERVER_PORT` | No | `7860` | MCP server port for controlled pipeline |
| `WORKER_ID` | No | `worker-{PID}` | Unique identifier for multi-worker setups |
| `HEALTH_CHECK_PORT` | No | `8001` | Worker health check HTTP port (set to `0` to disable) |
| `WORKER_HEALTH_URL` | No | `http://localhost:8001/health` | Worker health URL for admin status endpoint (Docker: `http://worker:8001/health`) |
| `USER_STORAGE_LIMIT_MB` | No | `2048` | Max cloud storage per user in MB (2 GB default) |
| `USER_DAILY_TASK_LIMIT` | No | `10` | Max tasks a user can submit per day (UTC midnight reset) |

## How It Works

### Auto Mode (default)

1. User submits a simulation prompt via the frontend
2. API server inserts a row into Supabase with `status='queued'`
3. Worker polls the table, atomically claims the job via `claim_next_job()` RPC (`FOR UPDATE SKIP LOCKED`), sets `status='running'`
4. Worker launches Foam-Agent as a subprocess with `cwd=FOAM_AGENT_DIR`
5. On completion: uploads all files + ZIP archive to Supabase Storage, sets `status='completed'`
6. On failure/cancellation: uploads whatever files exist + ZIP archive, sets `status='failed'` or `status='cancelled'`
7. Frontend receives real-time updates via Supabase Realtime

### Controlled Pipeline Mode (MCP)

Stage-by-stage execution with optional user checkpoints:

```
plan → [plan_review] → input_writer → [files_review] → pre-run → [pre_run_review] → full run → completed
```

Stages in brackets are optional checkpoints. At each checkpoint the frontend shows a review panel where the user can browse files, leave feedback, then confirm or reject. Pre-run includes an automatic fix loop (up to 5 retries) using MCP `review()` + `apply_fixes()`.

### Data Lifecycle

- Failed/cancelled tasks: auto-expire after 7 days
- Completed tasks: auto-expire after 14 days
- Soft-deleted tasks: permanently purged after 3 days (Storage files → local runs/ → DB rows)
- All files (success, failure, and cancellation) are uploaded to Supabase Storage with ZIP archive

### Account Management

- Profile auto-creation on first login (three-layer fallback: frontend insert → API endpoint with service_role)
- Account deletion endpoint (`DELETE /api/v1/users/me`): cascading cleanup of Storage files → simulations → user_profiles → Auth user

### BYOK (Bring Your Own Key)

Users can provide their own LLM config (provider, model, API key or Codex OAuth token). The worker injects it as environment variables and immediately deletes the key from the database.

### Multi-Worker Concurrency

Multiple workers can run safely in parallel — `claim_next_job()` RPC uses PostgreSQL `FOR UPDATE SKIP LOCKED` to prevent duplicate claims. Each worker gets a unique `WORKER_ID` for log identification.

### Health Check & Monitoring

Three layers of defense ensure the admin can quickly detect and recover from failures:

| Layer | Purpose | How |
|-------|---------|-----|
| Docker self-healing | Auto-restart crashed containers | `restart: unless-stopped` + `healthcheck` in docker-compose.yml |
| Admin status endpoint | One URL to check everything | `GET /api/v1/admin/status` returns API + Worker health |
| UptimeRobot | External alerting (email) | Monitors the admin status endpoint every 5 minutes |

**Worker health endpoint** — each worker exposes `GET /health` on `HEALTH_CHECK_PORT` (default 8001):

```bash
curl localhost:8001/health
# {"status":"idle","worker_id":"worker-123","jobs_processed":5,"jobs_succeeded":3,"jobs_failed":2,"current_job_id":null,"uptime_seconds":3600,"start_time":"..."}
```

**Admin status endpoint** — aggregates API + Worker health into a single check (no auth required):

```bash
curl localhost:8000/api/v1/admin/status
# All healthy (HTTP 200):
# {"api":"ok","worker":{"status":"idle","worker_id":"worker-123",...},"all_healthy":true}

# Worker down (HTTP 503):
# {"api":"ok","worker":{"status":"unreachable","error":"Connection refused"},"all_healthy":false}
```

**Troubleshooting workflow** when users report issues (e.g. tasks stuck in queue):

1. Open `https://your-domain/api/v1/admin/status` in a browser (no login needed)
2. Check `all_healthy`:
   - `true` — Worker is alive, investigate elsewhere (Supabase, user prompt, etc.)
   - `false` — Worker is down, proceed to step 3
3. SSH to server and restart: `docker compose restart worker`

With UptimeRobot configured, step 1 is automated — you get an email alert when something goes wrong, and another when it recovers.

## Maintenance Log

### 2026-03-29

- **Fixed MCP server ignoring BYOK LLM config** (`mcp_client.py`, `worker.py`): In controlled pipeline mode, the MCP server subprocess was started without `FOAMAGENT_MODEL_PROVIDER`, `FOAMAGENT_MODEL_VERSION`, or API key env vars, causing it to always default to `openai-codex`. BYOK users (e.g. DeepSeek) hit the Codex OAuth endpoint and failed with `HTTP 401 token_expired`. Fix: `MCPServerManager.start()` now accepts `llm_env` dict; `_ensure_mcp_server()` builds LLM env vars from the job's `llm_config` and auto-restarts the server when config changes between jobs.
- **Added heavy simulation pre-check** (`worker.py`): `_check_prompt()` now detects keywords for LES, DES, DPM, FWH/acoustics, FSI, reacting flow, and 3D VOF — injects a `[PLATFORM NOTE]` warning to use coarse mesh and `purgeWrite` to avoid disk/memory limits.
- **Codex token expiry email alert**: Set up `msmtp` + Gmail SMTP + daily cron (`codex-token-sync.sh check --cron`) to send email alert 2 days before token expires. Login still requires manual browser interaction (`./codex-token-sync.sh login`).
- **Fixed cron job path**: Previous cron pointed to wrong directory (`cfdqanda-server/codex-token-sync.sh`), corrected to project root (`cfdqanda/codex-token-sync.sh`).
- **Updated Foam-Agent WeChat community text**: Changed "maintainer" to "volunteer", added WeChat account as alternative to QR code scanning ([PR #24](https://github.com/csml-rpi/Foam-Agent/pull/24)).

## Tests

```bash
# Run tests inside the API container
docker compose exec api python -m pytest tests/ -v

# Or locally with pip install -r requirements-api.txt
python -m pytest tests/ -v
```
