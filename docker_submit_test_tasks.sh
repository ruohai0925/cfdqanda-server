#!/bin/bash
# submit_test_tasks.sh — Rapidly submit N test tasks for multi-worker testing
#
# Usage:
#   ./submit_test_tasks.sh              Submit 5 tasks (default)
#   ./submit_test_tasks.sh 3            Submit 3 tasks
#
# Inserts directly via Supabase service key (no JWT/login needed).
# Uses ruohai372@gmail.com account (user_id hardcoded).

set -euo pipefail

NUM_TASKS=${1:-5}

echo "=== Submitting ${NUM_TASKS} test tasks ==="

docker exec cfdqanda-api python3 -c "
import os, json
from supabase import create_client

supabase = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_SERVICE_KEY'])
USER_ID = '59248e6d-f5e9-4ac0-a956-fa7ee3d4a3bf'  # ruohai372@gmail.com

prompts = [
    'Simulate laminar flow over a flat plate with Re=1000, inlet velocity 1 m/s, plate length 1m.',
    'Simulate 2D lid-driven cavity flow with Re=100, cavity size 0.1m x 0.1m.',
    'Simulate flow through a simple pipe with diameter 0.05m, length 0.5m, inlet velocity 0.1 m/s.',
    'Simulate natural convection in a 2D square cavity with hot left wall (310K) and cold right wall (290K).',
    'Simulate laminar flow around a 2D cylinder with diameter 0.02m, inlet velocity 0.5 m/s.',
    'Simulate Poiseuille flow in a 2D channel, height 0.01m, length 0.1m, pressure gradient driven.',
    'Simulate flow over a backward-facing step with expansion ratio 2, inlet velocity 0.5 m/s.',
    'Simulate 2D mixing layer between two parallel streams at velocities 1 m/s and 0.5 m/s.',
]

num = ${NUM_TASKS}
for i in range(num):
    prompt = prompts[i % len(prompts)]
    resp = supabase.table('simulations').insert({
        'prompt': f'[Test {i+1}/{num}] {prompt}',
        'user_id': USER_ID,
        'status': 'queued',
        'pipeline_mode': 'auto',
    }).execute()
    task = resp.data[0]
    print(f\"  Task {i+1}/{num}: id={task['id']} queued — {prompt[:60]}...\")

print()
print(f'All {num} tasks submitted.')
"

echo ""
echo "=== Monitor with: ==="
echo "  docker compose logs -f worker | grep -E 'Claimed|completed|failed|WORKER'"
