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
| `api_server.py` | FastAPI application — 11 REST endpoints for task management, file browsing, feedback, and storage |
| `worker.py` | Background worker — polls Supabase for jobs, launches Foam-Agent, uploads results, manages data lifecycle |
| `mcp_client.py` | MCP client for controlled pipeline mode (plan, input_writer, run, review, apply_fixes, visualization) |
| `allrun_validator.py` | Security audit for OpenFOAM Allrun scripts (whitelist-based command validation) |
| `token_extractor.py` | Extracts LLM token usage statistics from simulation logs |
| `.env.example` | Environment variables template |
| `tests/` | 13 test files covering auth, security, concurrency, pipeline, etc. |

## Prerequisites

- Foam-Agent repository cloned locally
- Conda environments: `foam-api` (API server), `FoamAgent` (worker)
- Supabase project with `simulations` table and `simulation_results` storage bucket
- OpenFOAM v10 installed

## Setup

```bash
cp .env.example .env
# Fill in all values, especially FOAM_AGENT_DIR
```

## Running

### API Server

```bash
conda activate foam-api
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

### Worker

```bash
conda activate FoamAgent
python -u worker.py
```

### Background mode

```bash
nohup uvicorn api_server:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &
nohup python -u worker.py > worker.log 2>&1 &
```

### Stopping

```bash
pkill -f "python.*worker\.py"
pkill -f "uvicorn api_server:app"
```

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/` | — | Health check |
| `POST` | `/api/v1/simulations` | JWT | Create a new simulation task |
| `GET` | `/api/v1/simulations/{id}/files` | JWT | Get file tree for a job |
| `POST` | `/api/v1/simulations/{id}/feedback` | JWT | Submit feedback on a specific file |
| `PATCH` | `/api/v1/simulations/{id}/rating` | JWT | Submit task-level rating (1-3) |
| `DELETE` | `/api/v1/simulations/{id}` | JWT | Soft-delete a task |
| `POST` | `/api/v1/simulations/{id}/cancel` | JWT | Cancel a running task |
| `POST` | `/api/v1/simulations/{id}/stage/confirm` | JWT | Confirm a pipeline checkpoint |
| `POST` | `/api/v1/simulations/{id}/stage/reject` | JWT | Reject a pipeline checkpoint |
| `POST` | `/api/v1/simulations/{id}/restore` | JWT | Restore a soft-deleted task |
| `GET` | `/api/v1/user/storage` | JWT | Get user's cloud storage usage summary |

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

## How It Works

### Auto Mode (default)

1. User submits a simulation prompt via the frontend
2. API server inserts a row into Supabase with `status='queued'`
3. Worker polls the table, picks up the job, sets `status='running'`
4. Worker launches Foam-Agent as a subprocess with `cwd=FOAM_AGENT_DIR`
5. On completion: uploads all files to Supabase Storage, sets `status='completed'`
6. On failure: uploads whatever files exist, sets `status='failed'` with error details
7. Frontend receives real-time updates via Supabase Realtime

### Controlled Pipeline Mode (MCP)

Stage-by-stage execution with optional user checkpoints:

```
plan → [plan_review] → input_writer → [files_review] → pre-run → [pre_run_review] → full run → completed
```

Stages in brackets are optional checkpoints. At each checkpoint the frontend shows a review panel where the user can browse files, leave feedback, then confirm or reject.

### Data Lifecycle

- Failed tasks: auto-expire after 7 days
- Completed tasks: auto-expire after 14 days
- Soft-deleted tasks: permanently purged after 3 days
- All files (success and failure) are uploaded to Supabase Storage

### BYOK (Bring Your Own Key)

Users can provide their own LLM config (provider, model, API key). The worker injects it as environment variables and immediately deletes the key from the database.

## Tests

```bash
conda activate foam-api
python -m pytest tests/ -v
```
