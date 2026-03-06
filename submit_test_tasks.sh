#!/bin/bash
# submit_test_tasks.sh — Rapidly submit N test tasks for multi-worker testing
#
# Usage:
#   ./submit_test_tasks.sh              Submit 5 tasks (default)
#   ./submit_test_tasks.sh 3            Submit 3 tasks
#
# Requires: API server running on localhost:8000, valid JWT token.
# Uses ruohai372@gmail.com account by default.

set -euo pipefail

NUM_TASKS=${1:-5}
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
API_URL="http://localhost:8000"

# Source .env for Supabase credentials
if [ -f "$SCRIPT_DIR/.env" ]; then
    SUPABASE_URL=$(grep -E '^SUPABASE_URL=' "$SCRIPT_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
    SUPABASE_ANON_KEY=$(grep -E '^SUPABASE_ANON_KEY=' "$SCRIPT_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

if [ -z "${SUPABASE_URL:-}" ] || [ -z "${SUPABASE_ANON_KEY:-}" ]; then
    echo "ERROR: SUPABASE_URL and SUPABASE_ANON_KEY must be set in .env"
    echo "Add SUPABASE_ANON_KEY (the anon/public key) to .env for this script."
    exit 1
fi

# Get email/password from args or prompt
EMAIL="${TEST_EMAIL:-}"
PASSWORD="${TEST_PASSWORD:-}"

if [ -z "$EMAIL" ]; then
    read -rp "Email: " EMAIL
fi
if [ -z "$PASSWORD" ]; then
    read -rsp "Password: " PASSWORD
    echo ""
fi

# Login to get JWT token
echo "Logging in as $EMAIL..."
LOGIN_RESP=$(curl -s -X POST "$SUPABASE_URL/auth/v1/token?grant_type=password" \
    -H "apikey: $SUPABASE_ANON_KEY" \
    -H "Content-Type: application/json" \
    -d "{\"email\": \"$EMAIL\", \"password\": \"$PASSWORD\"}")

ACCESS_TOKEN=$(echo "$LOGIN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('access_token',''))" 2>/dev/null)

if [ -z "$ACCESS_TOKEN" ]; then
    echo "ERROR: Login failed. Response:"
    echo "$LOGIN_RESP" | python3 -m json.tool 2>/dev/null || echo "$LOGIN_RESP"
    exit 1
fi

echo "Login successful. Submitting $NUM_TASKS tasks..."
echo ""

# Simple test prompts (lightweight CFD cases)
PROMPTS=(
    "Simulate laminar flow over a flat plate with Re=1000, inlet velocity 1 m/s, plate length 1m."
    "Simulate 2D lid-driven cavity flow with Re=100, cavity size 0.1m x 0.1m."
    "Simulate flow through a simple pipe with diameter 0.05m, length 0.5m, inlet velocity 0.1 m/s."
    "Simulate natural convection in a 2D square cavity with hot left wall (310K) and cold right wall (290K)."
    "Simulate laminar flow around a 2D cylinder with diameter 0.02m, inlet velocity 0.5 m/s."
    "Simulate Poiseuille flow in a 2D channel, height 0.01m, length 0.1m, pressure gradient driven."
    "Simulate flow over a backward-facing step with expansion ratio 2, inlet velocity 0.5 m/s."
    "Simulate 2D mixing layer between two parallel streams at velocities 1 m/s and 0.5 m/s."
)

for i in $(seq 1 "$NUM_TASKS"); do
    idx=$(( (i - 1) % ${#PROMPTS[@]} ))
    PROMPT="${PROMPTS[$idx]}"

    RESP=$(curl -s -X POST "$API_URL/api/v1/simulations" \
        -H "Authorization: Bearer $ACCESS_TOKEN" \
        -H "Content-Type: application/json" \
        -d "{\"prompt\": \"$PROMPT\"}")

    STATUS=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('status','error'))" 2>/dev/null)
    JOB_ID=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('job_id','?'))" 2>/dev/null)

    echo "  Task $i/$NUM_TASKS: $STATUS (job_id=$JOB_ID) — ${PROMPT:0:60}..."
done

echo ""
echo "All $NUM_TASKS tasks submitted. Workers should pick them up."
echo ""
echo "Monitor progress:"
echo "  tail -f worker-*.log | grep -E 'Claimed|completed|failed'"
