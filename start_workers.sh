#!/bin/bash
# start_workers.sh — Scale Worker containers for concurrency testing
#
# Usage:
#   ./start_workers.sh              Start 2 workers (default)
#   ./start_workers.sh 3            Start 3 workers
#
# Each worker container gets its own MCP server (isolated network namespace).
# Jobs are claimed atomically via PostgreSQL FOR UPDATE SKIP LOCKED — no duplicates.
#
# Stop / scale back:
#   docker compose up -d --scale worker=1

set -euo pipefail

NUM_WORKERS=${1:-2}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Scaling to ${NUM_WORKERS} worker(s) ==="
docker compose up -d --scale worker="$NUM_WORKERS"

echo ""
echo "=== Container status ==="
docker compose ps

echo ""
echo "=== Memory usage ==="
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}\t{{.MemPerc}}" | grep -E "NAME|worker|api"

echo ""
echo "=== Monitor logs ==="
echo "  docker compose logs -f worker | grep -E 'Claimed|completed|failed|WORKER'"
echo ""
echo "=== Scale back ==="
echo "  docker compose up -d --scale worker=1"
