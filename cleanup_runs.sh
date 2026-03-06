#!/bin/bash
# cleanup_runs.sh — Manual cleanup of local Foam-Agent runs/ directories
#
# Usage:
#   ./cleanup_runs.sh --all                  Delete ALL runs
#   ./cleanup_runs.sh --id 107               Delete single job
#   ./cleanup_runs.sh --id 107,109,115       Delete multiple jobs (comma-separated)
#   ./cleanup_runs.sh --range 100-150        Delete jobs in ID range (inclusive)
#   ./cleanup_runs.sh --before 100           Delete all jobs with ID < 100
#   ./cleanup_runs.sh --largest 5             Delete the 5 largest runs
#   ./cleanup_runs.sh --dry-run --all        Preview what would be deleted (no actual deletion)
#   ./cleanup_runs.sh --dry-run --largest 10 Preview the 10 largest runs
#
# The script reads FOAM_AGENT_DIR from .env if available, otherwise
# falls back to the FOAM_AGENT_DIR environment variable.

set -euo pipefail

# --- Resolve FOAM_AGENT_DIR ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -z "${FOAM_AGENT_DIR:-}" ] && [ -f "$SCRIPT_DIR/.env" ]; then
    FOAM_AGENT_DIR=$(grep -E '^FOAM_AGENT_DIR=' "$SCRIPT_DIR/.env" | cut -d= -f2- | tr -d '"' | tr -d "'")
fi

if [ -z "${FOAM_AGENT_DIR:-}" ]; then
    echo "ERROR: FOAM_AGENT_DIR is not set. Set it in .env or as an environment variable."
    exit 1
fi

RUNS_DIR="$FOAM_AGENT_DIR/runs"
if [ ! -d "$RUNS_DIR" ]; then
    echo "ERROR: Runs directory not found: $RUNS_DIR"
    exit 1
fi

DRY_RUN=false
MODE=""
ARG=""

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)  DRY_RUN=true; shift ;;
        --all)      MODE="all"; shift ;;
        --id)       MODE="id"; ARG="$2"; shift 2 ;;
        --range)    MODE="range"; ARG="$2"; shift 2 ;;
        --before)   MODE="before"; ARG="$2"; shift 2 ;;
        --largest)  MODE="largest"; ARG="$2"; shift 2 ;;
        -h|--help)
            echo "Usage:"
            echo "  $0 --all                  Delete ALL runs"
            echo "  $0 --id 107               Delete single job"
            echo "  $0 --id 107,109,115       Delete multiple jobs"
            echo "  $0 --range 100-150        Delete jobs in ID range (inclusive)"
            echo "  $0 --before 100           Delete all jobs with ID < 100"
            echo "  $0 --largest 5             Delete the 5 largest runs"
            echo "  $0 --dry-run <mode>       Preview without deleting"
            exit 0
            ;;
        *)
            echo "Unknown argument: $1 (use --help)"
            exit 1
            ;;
    esac
done

if [ -z "$MODE" ]; then
    echo "ERROR: No mode specified. Use --all, --id, --range, or --before."
    echo "Run with --help for usage."
    exit 1
fi

# --- Collect directories to delete ---
declare -a TO_DELETE=()

case "$MODE" in
    all)
        for d in "$RUNS_DIR"/*/; do
            [ -d "$d" ] && TO_DELETE+=("$d")
        done
        ;;
    id)
        IFS=',' read -ra IDS <<< "$ARG"
        for id in "${IDS[@]}"; do
            id=$(echo "$id" | tr -d ' ')
            target="$RUNS_DIR/$id"
            if [ -d "$target" ]; then
                TO_DELETE+=("$target")
            else
                echo "WARNING: $target does not exist, skipping."
            fi
        done
        ;;
    range)
        IFS='-' read -r START END <<< "$ARG"
        if [ -z "$START" ] || [ -z "$END" ]; then
            echo "ERROR: Invalid range format. Use --range START-END (e.g. --range 100-150)"
            exit 1
        fi
        for d in "$RUNS_DIR"/*/; do
            [ -d "$d" ] || continue
            name=$(basename "$d")
            # Only process numeric directory names
            if [[ "$name" =~ ^[0-9]+$ ]]; then
                if [ "$name" -ge "$START" ] && [ "$name" -le "$END" ]; then
                    TO_DELETE+=("$d")
                fi
            fi
        done
        ;;
    before)
        for d in "$RUNS_DIR"/*/; do
            [ -d "$d" ] || continue
            name=$(basename "$d")
            if [[ "$name" =~ ^[0-9]+$ ]] && [ "$name" -lt "$ARG" ]; then
                TO_DELETE+=("$d")
            fi
        done
        ;;
    largest)
        # Sort all runs by size (largest first), pick top N
        while IFS=$'\t' read -r _ dir; do
            TO_DELETE+=("$dir")
        done < <(
            for d in "$RUNS_DIR"/*/; do
                [ -d "$d" ] || continue
                bytes=$(du -sb "$d" 2>/dev/null | awk '{print $1}')
                printf '%s\t%s\n' "$bytes" "$d"
            done | sort -rn | head -n "$ARG"
        )
        ;;
esac

# --- Summary ---
COUNT=${#TO_DELETE[@]}
if [ "$COUNT" -eq 0 ]; then
    echo "Nothing to delete."
    exit 0
fi

# Calculate total size
TOTAL_SIZE=$(du -sh "${TO_DELETE[@]}" 2>/dev/null | tail -1 | awk '{print $1}')
if [ "$COUNT" -gt 1 ]; then
    TOTAL_SIZE=$(du -shc "${TO_DELETE[@]}" 2>/dev/null | tail -1 | awk '{print $1}')
fi

echo "Found $COUNT directories to delete ($TOTAL_SIZE total):"
for d in "${TO_DELETE[@]}"; do
    size=$(du -sh "$d" 2>/dev/null | awk '{print $1}')
    echo "  $(basename "$d")  ($size)"
done

if [ "$DRY_RUN" = true ]; then
    echo ""
    echo "[DRY RUN] No files were deleted."
    exit 0
fi

# --- Confirm and delete ---
echo ""
read -rp "Delete these $COUNT directories? [y/N] " confirm
if [[ "$confirm" =~ ^[Yy]$ ]]; then
    for d in "${TO_DELETE[@]}"; do
        rm -rf "$d"
        echo "  Deleted: $(basename "$d")"
    done
    echo "Done. Freed $TOTAL_SIZE."
else
    echo "Cancelled."
fi
