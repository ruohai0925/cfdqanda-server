#!/usr/bin/env bash
# kill-worker-api.sh — Stop all containers (API + Workers)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Stopping all containers ==="
docker compose down

echo ""
echo "=== Done. All containers stopped. ==="
