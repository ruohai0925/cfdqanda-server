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

### 2026-05-23

- **Diagnosed silent data wipe during a one-month absence** (Tasks 469–508): The first weekly review after returning showed `**该时间段内没有任务。**` even though the id sequence had advanced to 509 (~508 tasks ever created). Investigation: `simulations` table empty, but Supabase Storage still held 45 task dirs / 2262 files / 665 MB across 8 users. Root cause traced to **worker.py's own TTL purge**, running continuously while nobody was watching: failed/cancelled rows soft-deleted at 7 days and hard-deleted 3 days later; completed rows at 14+3. By ~2026-04-26 every historical row had been hard-deleted. Storage was preserved only because the conditional Storage cleanup (`if storage_base_path:`) was a no-op for older rows whose `result_data` predated that field — yet the **unconditional** DB row delete still ran, producing the orphan-Storage symptom that hid the purge from view for a month.
- **Worker TTL relaxed** (`worker.py`): `TTL_FAILED_DAYS` 7 → 30, `TTL_COMPLETED_DAYS` 14 → 90, `PURGE_RETENTION_DAYS` 3 → 7. Net retention is now 37 days for failed/cancelled and 97 days for completed — comfortably survives a month-long gap.
- **Hard-delete now verifies Storage cleanup before deleting the DB row** (`worker.py:_purge_deleted_simulations`): If `result_data.storage_base_path` is missing, fall back to `public/{user_id}/{job_id}` (the canonical upload path used everywhere else in `worker.py`). After `_remove_storage_directory()`, re-`list()` the prefix and refuse to delete the DB row if any entries remain. Mesh-file cleanup is also a gate. Local `runs/` cleanup stays best-effort and never blocks DB delete. The orphan-Storage scenario can no longer happen.
- **Per-task disk limit 400 MB → 1024 MB** (`worker.py`, `.env.example`): Task #325 (`cg.liang@nuaa.edu.cn`, 21700 battery thermal simulation, `laplacianFoam`) ran 11 Rewrite loops and hit `[Errno 28] No space left on device` while close to convergence — agent reasoning was sound, the budget was just too tight. New default documented in `.env.example` as `TASK_DISK_LIMIT_MB=1024`.
- **Worker-startup Codex token healthcheck** (`worker.py:_check_platform_codex_token`): Parses `$CODEX_HOME/auth.json` as a JWT, decodes the `exp` claim, logs `ERROR` / `WARN (expiring < 24h)` / `INFO` based on health. Surfaces the 2026-04-09 failure class (Tasks 453/454/455 hit `HTTP 401 token_expired` simultaneously; Task 468 hit `HTTP 500` an hour later) at boot rather than silently burning user quota. After post-fix restart, the log confirmed `Platform Codex token: token valid until 2026-05-15`. Tested in isolation against 6 fake JWTs (healthy / expiring_soon / expired / missing / malformed / not-a-JWT).
- **Daily quota refund for platform-side failures** (`api_server.py`): Added `PLATFORM_REFUND_CATEGORIES = ['auth_error_platform', 'codex_quota_exceeded']` and a `_count_today_billable(user_id)` helper using `.in_()` on the JSONB path `result_data->>error_category`. Both the create-task quota check and `GET /user/daily-usage` now subtract refunded tasks from the count. The endpoint also exposes `total_today` and `refunded_platform_failures` for transparency. Verified end-to-end with 6 probe tasks (3 ok, 2 platform-fail, 1 byok-auth-fail) → billable = 4; `auth_error_byok` correctly still counts (user's key, user's problem).
- **`weekly_review.py` paginated `list_users()` + CI tolerance**: The default `per_page=50` was returning only the first 50 of our 151 auth users, leaving most emails blank in the report. Paginated with `per_page=200`. Also made `.env` loading optional so the script runs in CI from env vars alone.
- **GitHub Actions weekly review** (`.github/workflows/weekly_review.yml`): Scheduled every Monday 09:00 UTC, runs `weekly_review.py`, uploads the report as a 90-day artifact, and auto-opens a labeled GitHub issue. Validated via `workflow_dispatch` ([run 25353753830](https://github.com/ruohai0925/cfdqanda-server/actions/runs/25353753830)) — first issue (#1) auto-created. Direct lesson from this incident: month-long blind spots don't happen when a weekly check is automated.
- **`check_storage.py` paginated `list_users()` + Storage as ground truth**: Same pagination bug as `weekly_review.py` — was reporting 50 users instead of 151. Worse: total Storage usage was derived from `simulations.result_data.upload_stats.total_bytes`, which is gone after TTL purge, so the script reported **0 GB** while Storage actually held **665 MB / 2262 files**. Replaced with a direct walk of the `simulation_results` bucket. Cross-references DB statuses when rows still exist; shows `(no DB row)` otherwise. Adds an explicit warning when Storage > 0 but DB rows = 0.
- **Operational cleanup**: Cleared 805 MB of orphan local `runs/{job_id}` directories (83 dirs, all with DB rows long gone — preserved `test-*` manual-test dirs and `.gitkeep`). `docker builder prune -af` reclaimed 28.96 GB of build cache. Total local disk reclaimed ~30 GB. Restarted both worker replicas to pick up the worker.py changes — startup logs confirm `Task disk limit set to 1024 MB` and the codex token healthcheck output.
- **Embedded English platform walkthrough video on the User Guide page** (`cfdqanda-client/src/UserGuide.jsx`, `App.jsx`): YouTube tutorial (`_Fveasp8QHI`) embedded via the `youtube-nocookie` domain at the top of the guide, lazy-loaded, 16:9 responsive iframe. Footer link relabeled `User Guide` → `User Guide (incl. video)` / `用户指南（含视频）` so the video is discoverable from the existing entry without adding new top-level UI.
- **One-shot recovery utilities** (kept outside any repo, in `cfdqanda/`): `rebuild_history.py` walks Storage to reconstruct a read-only task index when DB rows are gone (recovered 45 historical tasks for offline reference). `system_review.py` analyzes the same data for failure modes and ranked improvement priorities — its output drove the changes in this entry. Not committed; one-shot diagnostic tools.
- **BYOK routing fix — three layers** (root cause for 2026-05-21 `zhuge7777@139.com` incident: 12 BYOK failures, all reported as "Incorrect API key" while the keys were actually fine): (1) `api_server.py` `LLMConfig` Pydantic model was missing the `base_url` field, so Pydantic silently dropped it before insertion — frontend had been sending it correctly since 2026-03-10 but it never reached the worker. Added `base_url: Optional[str] = None`. (2) `worker.py` `_build_llm_env` (controlled mode) and the auto-mode env builder now fall back to `PROVIDER_DEFAULT_BASE_URLS = {'qwen': '…/dashscope.aliyuncs.com/compatible-mode/v1', 'deepseek': 'https://api.deepseek.com'}` when `base_url` is missing for a known non-OpenAI provider — defence in depth so older clients / direct API users also route correctly. (3) `AISimulationTab.jsx` BYOK provider-change handler now auto-fills the provider's `isDefault` model instead of clearing to empty, so users can't accidentally submit provider=openai with model=qwen-plus (the actual 5/21 trigger).
- **DeepSeek V4 family flagged as currently unavailable in BYOK** (`cfdqanda-client/src/AISimulationTab.jsx`): tested all available DeepSeek models on 2026-05-23 cavity case — `deepseek-v4-pro`, `deepseek-v4-flash`, plus the deprecated `deepseek-chat`/`deepseek-reasoner` aliases. All fail at Foam-Agent's planner with HTTP 400 `"Thinking mode does not support this tool_choice"`. V4 forces thinking mode by default and thinking mode rejects `tool_choice`, which `with_structured_output(CaseSummaryModel)` requires. `MODEL_VERSIONS['deepseek']` updated to the actual API model list (`v4-pro`/`v4-flash`) with `(currently unavailable)` tags; a yellow warning banner shows when a user selects DeepSeek explaining the incompat and suggesting OpenAI/Anthropic. Issue logged in `docs/active/Foam-Agent-Comments.md` section 7 — upstream fix needs `LLMService` to expose `extra_body` passthrough so we can send `{"thinking": false}` for DeepSeek.
- **Platform default Codex model bumped `gpt-5.3-codex` → `gpt-5.5`** (`worker.py` 4 sites + tests + `analyze_cases.py` + `api_server.py` docstring + `AISimulationTab.jsx` selector + BYOK openai list + UI strings): GPT-5.5 (released 2026-04-23) absorbed the codex training stack — 82.7% vs 77.3% on Terminal-Bench 2.0, ~40% fewer tokens for the same task, same latency. ChatGPT Plus and above include it. Net upgrade with no downside. Frontend Codex dropdown reordered: `gpt-5.5 (default, recommended)` / `gpt-5.4` / `gpt-5.4-mini (fast)` / `gpt-5.3-codex (legacy)` / `gpt-5.2`; BYOK `openai` list adds `gpt-5.5` as `isDefault`. Memory `MEMORY.md` `## Platform Default Model` section updated to match.

### 2026-04-09

- **Diagnosed why controlled mode systematically underperforms auto mode** (Tasks 433/443/458/459/465/466/478): User feedback from fuadhhasan@RPI on Task 433 (`"Case 433 failed using the interactive mode; yet case 434 succeeded using the e2e mode."`) led to a 4-bug root-cause chain. Same buoyantFoam prompt produced different `0/alphat`, `system/fvSchemes`, and `system/fvSolution` files in the two modes — controlled mode consistently missed `compressible::` namespace prefixes, `div(phi,K)`, `div(phi,Ekp)`, and the compressible `div(((rho*nuEff)*dev2(T(grad(U)))))` form.
- **Bug A — `_OF_VERSION_RE` false positive** (`worker.py`): The regex `(?:openfoam|of)\s*...(\d+)` matched `"temperature of 300K"` as `OpenFOAM 300`. Tightened to `\bopenfoam[-\s_]*v?(\d{1,2})(?!\d)`, requiring the full word "openfoam" and limiting to 1-2 digit versions. The previous false positive accidentally helped auto mode by triggering a version-mismatch warning that contained the alphat namespace hint, while controlled mode (which never called `_check_prompt`) saw nothing.
- **Bug B — v10 syntax hints permanently injected into `_PLATFORM_NOTE`** (`worker.py`): The critical v10 gotchas (`compressible::alphatJayatillekeWallFunction` namespace, `stopAt endTime`, `Gauss upwind`, `rhoFinal`/`pFinal`/`p_rghFinal`/etc. for PIMPLE, `div(phi,K)`/`div(phi,Ekp)` for kinetic and total energy projection, compressible `div(((rho*nuEff)*dev2(T(grad(U)))))` form, and a warning against `#codeStream` runtime C++ compilation which is blocked under Docker root) are now always present in the platform note instead of buried inside an accidentally-fired version-mismatch warning. Note tightened from ~1700 to 1223 chars.
- **Bug C — controlled mode now calls `_check_prompt`** (`worker.py:_handle_controlled_pipeline`): Previously the controlled pipeline forwarded the raw `job['prompt']` directly to `client.plan()` / `client.input_writer()` / `client.review()` / `client.apply_fixes()`, completely bypassing platform-note augmentation. Fix: compute `augmented_prompt = job['prompt'] + [PLATFORM NOTE]` once on first touch, stash on `pipeline_state['augmented_prompt']` (jsonb-persisted so checkpoint resumes can reuse it), and use it in all four MCP calls. Verified via Job 458 — `pipeline_state.augmented_prompt` is now correctly populated with the full v10 hints.
- **Bug D — filed and end-to-end verified upstream as [csml-rpi/Foam-Agent#28](https://github.com/csml-rpi/Foam-Agent/pull/28)**: The MCP `input_writer` tool in `fastmcp_server.py` discards `similar_case_advice` (the 5th return value of `retrieve_references()`) by assigning it to `_`. As a result, MCP-driven file generation receives strictly less context than the in-process LangGraph pipeline (which forwards `state["similar_case_advice"]` to `initial_write()`). 2-line minimal fix submitted. **Verified locally** by checking out the PR branch on both local and cloud Foam-Agent dirs and running two MCP-mode tasks: (1) **Job 479** — Task 433's exact `buoyantFoam` prompt that previously failed in 5 separate controlled-mode attempts now `completed` in 0 fix iterations with a complete `system/fvSchemes` (correct `compressible::alphatJayatillekeWallFunction`, `div(phi,K)`, `div(phi,Ekp)`, compressible `div(((rho*nuEff)*dev2(T(grad(U)))))` form), full 100 s of physical time, 78 files / 3.5 MB output; (2) **Job 480** — incompressible 2D lid-driven cavity (`icoFoam`) regression check, also `completed` (37 files / 282 KB) confirming no regression on non-thermal cases. PR test-plan checkboxes ticked and verification comment posted ([#issuecomment-4217151169](https://github.com/csml-rpi/Foam-Agent/pull/28#issuecomment-4217151169)). Once merged upstream, our `_PLATFORM_NOTE` buoyantFoam-specific hints can be largely removed.
- **Pre-run fix loop improvements** (`worker.py:_mcp_stage_pre_run`): (1) `PRE_RUN_MAX_FIX_ATTEMPTS` is now an env var (default raised from 5 → 8). (2) Added an early-stop: if errors stay byte-identical for 2 consecutive iterations, the loop bails as `pre_run_no_progress` instead of wasting more LLM tokens. (3) Failure messages distinguish "exhausted attempts" from "no progress".
- **Checkpoint timeout 30 min → 2 h** (`worker.py`): `CHECKPOINT_TIMEOUT` default raised from 1800 → 7200 seconds. Task 444 (879329937@qq.com) lost their controlled-mode work because the previous 30-minute window was too aggressive for users actually inspecting generated files at `files_review`/`pre_run_review` checkpoints.
- **Auth error message split into BYOK vs platform variants** (`worker.py:_diagnose_error_text`, `tests/test_step049_error_diagnosis.py`): The previous message `"LLM API authentication failed. Please check your API key."` was misleading for users on the platform default (who have no API key to check). New variants: `auth_error_byok` (`"Your API key/Codex token is invalid or expired..."`) and `auth_error_platform` (`"Platform default model is temporarily unavailable; the administrator has been notified..."`). Refactored into `_diagnose_error_text()` so controlled mode also catches these in its exception handler.
- **`weekly_review.py` now collects all 5 feedback layers**: Previously only read `simulations.user_rating + user_comment`. Fixed `stage_feedback` location bug (it lives in `pipeline_state`, not `result_data`), added `stage_ratings` jsonb column, added `platform_feedback` table query, and added Storage `*_feedback` file scanning. Found a key Task 433 user comment that drove this entire investigation.
- **Codex BYOK token expiry hint** (`AISimulationTab.jsx`): Added a warning under the optional Codex Token field explaining that Codex OAuth tokens expire every ~10 days and require re-login through the ChatGPT client; recommends switching to BYOK with a regular API key for stable long-term use.
- **End-to-end verification — Job 478**: Submitted Task 433's exact prompt (buoyant thermal flow in 10×5×10 room with 600K hotspot) in auto mode. ✅ Completed successfully (7 MB output, 114 files, 100s of physical time). The combination of platform note injection + `#codeStream` warning fixes the original failure.

### 2026-03-29

- **BYOK (Bring Your Own Key) verified**: First successful BYOK task (Task 414, openai/gpt-4o). Confirmed end-to-end: frontend submission → worker env var injection → Foam-Agent execution → result upload. API key correctly cleared from DB after pickup.
- **Self-service invitation code board** (`InvitationBoard.jsx`, `App.jsx`, `Auth.jsx`): Public page at `#invite` displaying 20 invitation codes with copy buttons. Used codes shown as grayed out. Auto-generates a new batch via Supabase RPC (`list_invitation_codes_with_replenish`) when all codes are claimed. Registration form links to the board with "Get a code here".
- **Fixed MCP server ignoring BYOK LLM config** (`mcp_client.py`, `worker.py`): In controlled pipeline mode, the MCP server subprocess was started without `FOAMAGENT_MODEL_PROVIDER`, `FOAMAGENT_MODEL_VERSION`, or API key env vars, causing it to always default to `openai-codex`. BYOK users (e.g. DeepSeek) hit the Codex OAuth endpoint and failed with `HTTP 401 token_expired`. Fix: `MCPServerManager.start()` now accepts `llm_env` dict; `_ensure_mcp_server()` builds LLM env vars from the job's `llm_config` and auto-restarts the server when config changes between jobs.
- **Added heavy simulation pre-check** (`worker.py`): `_check_prompt()` now detects keywords for LES, DES, DPM, FWH/acoustics, FSI, reacting flow, and 3D VOF — injects a `[PLATFORM NOTE]` recommending coarse mesh and `purgeWrite` given the platform's resource constraints.
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
