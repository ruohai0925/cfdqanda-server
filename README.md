# cfdqanda-server

> **License:** [PolyForm Strict 1.0.0](https://polyformproject.org/licenses/strict/1.0.0/) — source available for personal and non-commercial use only. Commercial use is prohibited.

Platform server layer for [CFDQandA](https://cfdqanda.com) — a natural-language-driven CFD simulation platform.

This repository contains the **API server** (FastAPI) and **background worker** that bridge the React frontend with the [Foam-Agent](https://github.com/YYgroup/Foam-Agent) simulation engine.

## Architecture

```
Browser (React)
    │
    ▼
API Server (FastAPI, port 8000)
    │
    ▼
Supabase (PostgreSQL + Storage + Realtime + Auth)
    ▲
    │
Worker (polling loop)
    │
    ▼
Foam-Agent subprocess (LangGraph → OpenFOAM)
```

The server does **not** contain any simulation logic. It only:

1. Receives task requests from the frontend and inserts them into Supabase
2. Polls Supabase for queued tasks and launches Foam-Agent as a subprocess
3. Uploads results to Supabase Storage after completion

## Files

| File | Description |
|------|-------------|
| `api_server.py` | FastAPI application — endpoints for task creation, file tree retrieval, and feedback submission |
| `worker.py` | Background worker — polls Supabase for queued jobs, launches Foam-Agent subprocess, uploads results |
| `app.py` | Minimal health-check endpoint (for MCP transport) |
| `.env` | Environment variables (not committed) |
| `.env.example` | Template for `.env` |

## Prerequisites

- **Foam-Agent** repository cloned locally (this server calls it via subprocess)
- **Conda environments** set up:
  - `foam-api` — for running the API server (`fastapi`, `uvicorn`, `supabase`, `python-dotenv`)
  - `FoamAgent` — for running the worker (inherits Foam-Agent's full dependency stack)
- **Supabase** project with `simulations` table and `simulation_results` storage bucket
- **OpenFOAM v10** installed (required by Foam-Agent)

## Setup

1. Copy `.env.example` to `.env` and fill in all values:

```bash
cp .env.example .env
```

2. The most important variable is `FOAM_AGENT_DIR` — set it to the **absolute path** of the Foam-Agent directory:

```
FOAM_AGENT_DIR=/home/youruser/path/to/Foam-Agent
```

## Running

### API Server

```bash
cd cfdqanda-server
conda activate foam-api
uvicorn api_server:app --host 0.0.0.0 --port 8000
```

### Worker

```bash
cd cfdqanda-server
conda activate FoamAgent
python -u worker.py
```

### Background mode (production)

```bash
nohup uvicorn api_server:app --host 0.0.0.0 --port 8000 > api.log 2>&1 &
nohup python -u worker.py > worker.log 2>&1 &
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Health check |
| `POST` | `/api/v1/simulations` | Create a new simulation task |
| `GET` | `/api/v1/simulations/{job_id}/files` | Get file tree for a completed job |
| `POST` | `/api/v1/simulations/{job_id}/feedback` | Submit feedback on a specific file |

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `FOAM_AGENT_DIR` | Yes | Absolute path to the Foam-Agent repository |
| `SUPABASE_URL` | Yes | Supabase project URL |
| `SUPABASE_SERVICE_KEY` | Yes | Supabase service_role key (bypasses RLS) |
| `OPENAI_API_KEY` | No | Default OpenAI API key (used when user does not provide their own) |
| `WM_PROJECT_DIR` | No | OpenFOAM installation path (default: `/opt/openfoam10`) |
| `EXTRA_CORS_ORIGINS` | No | Additional CORS origins, comma-separated |

## How It Works

1. User submits a simulation prompt via the frontend
2. API server inserts a row into Supabase `simulations` table with `status='queued'`
3. Worker polls the table every 10 seconds, picks up the job, sets `status='running'`
4. Worker writes `prompt.txt` to `Foam-Agent/runs/{job_id}/` and launches Foam-Agent as a subprocess with `cwd=FOAM_AGENT_DIR`
5. On success: worker builds a file tree, creates a ZIP archive, uploads everything to Supabase Storage, sets `status='completed'`
6. On failure: worker sets `status='failed'` with error details
7. Frontend receives real-time status updates via Supabase Realtime

### BYOK (Bring Your Own Key)

Users can optionally provide their own LLM configuration (provider, model version, API key) when submitting a task. The worker injects these as environment variables into the Foam-Agent subprocess and **immediately deletes** the API key from the database after reading it.
