#!/usr/bin/env bash
# rebuild-worker-api.sh — Rebuild images and restart containers
# Usage: ./rebuild-worker-api.sh [num_workers]   (default: 1)
set -euo pipefail

NUM_WORKERS=${1:-1}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Rebuilding cfdqanda-worker image ==="
docker build -f Dockerfile.worker -t cfdqanda-worker .

echo ""
echo "=== Rebuilding cfdqanda-api image ==="
docker build -f Dockerfile.api -t cfdqanda-api .

echo ""
echo "=== Restarting api + ${NUM_WORKERS} worker(s) ==="
docker compose up -d --force-recreate --scale worker="$NUM_WORKERS" worker api

echo ""
echo "=== Done. Logs: ==="
docker compose logs -f --tail=30 worker api
