#!/usr/bin/env python3
"""backfill_archive.py — rescue what is left of the purged simulation history.

By 2026-09-22 the TTL purge had removed ~90% of all simulations ever created
(766 created, 76 left in the table) with no archive behind it, and Supabase
backups only reach back 7 days. Two sources still hold evidence:

  1. the live `simulations` rows                      -> full fidelity
  2. Supabase Storage, where 129 task folders survive -> id, owner, timestamp,
     file count/bytes, and for most of them prompt.txt, which is enough to
     recover the CFD domain tags

This writes both into `simulation_archive` (id-keyed upsert, so re-running is
safe). Storage-derived rows land first and live rows overwrite them, since the
live row is the better record.

Usage:
    python3 backfill_archive.py [--dry-run] [--no-storage]
"""

import argparse
import json
import sys

import taxonomy
from supabase_rest import Supa

BUCKET = "simulation_results"
LIVE_COLUMNS = ("id,user_id,created_at,updated_at,status,prompt,llm_config,"
                "pipeline_mode,timeout_minutes,user_rating,result_data")


def record_from_live(row):
    rd = row.get("result_data") or {}
    lc = row.get("llm_config") or {}
    stats = rd.get("upload_stats") or {}
    shape = taxonomy.summarize(row.get("prompt"))
    return {
        "id": row["id"],
        "user_id": row.get("user_id"),
        "created_at": row.get("created_at"),
        "finished_at": row.get("updated_at"),
        "status": row.get("status"),
        "error_category": rd.get("error_category"),
        "model_provider": lc.get("model_provider"),
        "model_version": lc.get("model_version"),
        "pipeline_mode": row.get("pipeline_mode"),
        "timeout_minutes": row.get("timeout_minutes"),
        "prompt_len": shape["prompt_len"],
        "prompt_lang": shape["prompt_lang"],
        "domain_tags": shape["domain_tags"],
        "is_platform_test": shape["is_platform_test"],
        "files_uploaded": stats.get("uploaded"),
        "bytes_uploaded": stats.get("total_bytes"),
        "user_rating": row.get("user_rating"),
        "archive_source": "backfill_db",
    }


def scan_storage(sb, skip_ids, verbose=True):
    """Reconstruct archive rows from surviving Storage folders."""
    records = []
    users = [u["name"] for u in sb.storage_list(BUCKET, "public")]
    if verbose:
        print(f"  Storage: {len(users)} user folder(s)")
    for uid in users:
        for job in sb.storage_list(BUCKET, f"public/{uid}"):
            job_id = job["name"]
            if not job_id.isdigit() or int(job_id) in skip_ids:
                continue
            prefix = f"public/{uid}/{job_id}"
            items = sb.storage_list(BUCKET, prefix)
            created, files, nbytes, has_prompt = None, 0, 0, False
            for it in items:
                if it.get("id"):
                    files += 1
                    nbytes += (it.get("metadata") or {}).get("size") or 0
                    ts = it.get("created_at")
                    if ts and (created is None or ts < created):
                        created = ts
                    if it["name"] == "prompt.txt":
                        has_prompt = True
            if created is None:                       # only sub-directories here
                for it in items:
                    for sub in sb.storage_list(BUCKET, f"{prefix}/{it['name']}"):
                        ts = sub.get("created_at")
                        if ts and (created is None or ts < created):
                            created = ts
                    if created:
                        break
            prompt = ""
            if has_prompt:
                try:
                    prompt = sb.storage_get(BUCKET, f"{prefix}/prompt.txt").decode("utf-8", "replace")
                except Exception as e:
                    print(f"    ! #{job_id}: prompt.txt unreadable ({e})")
            shape = taxonomy.summarize(prompt)
            records.append({
                "id": int(job_id),
                "user_id": uid,
                "created_at": created,
                "status": None,          # not recoverable from Storage
                "prompt_len": shape["prompt_len"] or None,
                "prompt_lang": shape["prompt_lang"],
                "domain_tags": shape["domain_tags"],
                "is_platform_test": shape["is_platform_test"],
                "files_uploaded": files or None,
                "bytes_uploaded": nbytes or None,
                "archive_source": "backfill_storage",
            })
    return records


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would be written")
    ap.add_argument("--no-storage", action="store_true", help="only archive live DB rows")
    args = ap.parse_args()

    sb = Supa()
    if not sb.table_exists("simulation_archive"):
        print("simulation_archive does not exist yet.\n"
              "Apply cfdqanda-server/sql/simulation_archive.sql in the Supabase SQL editor first.",
              file=sys.stderr)
        return 2

    live = sb.select("simulations", f"select={LIVE_COLUMNS}&order=id")
    live_records = [record_from_live(r) for r in live]
    live_ids = {r["id"] for r in live_records}
    print(f"Live rows: {len(live_records)}")

    storage_records = [] if args.no_storage else scan_storage(sb, live_ids)
    print(f"Storage-only rows recovered: {len(storage_records)}"
          f" ({sum(1 for r in storage_records if r['domain_tags'])} with domain tags)")

    if args.dry_run:
        print(json.dumps((storage_records + live_records)[:3], indent=1, ensure_ascii=False))
        print("(dry run — nothing written)")
        return 0

    # Storage first: a live row for the same id must win.
    sb.upsert("simulation_archive", storage_records)
    sb.upsert("simulation_archive", live_records)
    total = sb.select("simulation_archive", "select=id")
    print(f"simulation_archive now holds {len(total)} rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
