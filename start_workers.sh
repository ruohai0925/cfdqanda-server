#!/bin/bash
# start_workers.sh — Start multiple Worker processes for concurrency testing
#
# Usage:
#   ./start_workers.sh 3        Start 3 workers (default: 2)
#   ./start_workers.sh           Start 2 workers
#
# Each worker gets:
#   - Unique WORKER_ID (worker-1, worker-2, ...)
#   - Separate MCP_SERVER_PORT (7860, 7861, ...)
#   - Separate log file (worker-1.log, worker-2.log, ...)
#
# Stop all workers:
#   pkill -f "python -u worker.py"

set -euo pipefail

NUM_WORKERS=${1:-2}
BASE_MCP_PORT=7860
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_BASE=$(conda info --base 2>/dev/null)

if [ -z "$CONDA_BASE" ]; then
    echo "ERROR: conda not found. Make sure conda is available."
    exit 1
fi

echo "Starting $NUM_WORKERS worker(s)..."
echo ""

for i in $(seq 1 "$NUM_WORKERS"); do
    WORKER_ID="worker-$i"
    MCP_PORT=$((BASE_MCP_PORT + i - 1))
    LOG_FILE="$SCRIPT_DIR/$WORKER_ID.log"

    echo "  $WORKER_ID — MCP port $MCP_PORT, log: $LOG_FILE"

    WORKER_ID="$WORKER_ID" MCP_SERVER_PORT="$MCP_PORT" \
        bash -c "source $CONDA_BASE/etc/profile.d/conda.sh && conda activate FoamAgent && cd $SCRIPT_DIR && nohup python -u worker.py > $LOG_FILE 2>&1 &"
done

echo ""
echo "All $NUM_WORKERS workers started."
echo ""
echo "Monitor logs:"
for i in $(seq 1 "$NUM_WORKERS"); do
    echo "  tail -f $SCRIPT_DIR/worker-$i.log"
done
echo ""
echo "Monitor memory:"
echo "  watch -n 2 'ps aux --sort=-rss | grep \"python.*worker\" | grep -v grep'"
echo ""
echo "Stop all workers:"
echo "  pkill -f \"python -u worker.py\""
