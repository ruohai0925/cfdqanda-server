#!/usr/bin/env python3
"""
analyze_cases.py — Analyze all simulation cases from a given ID onward.

Usage:
    python analyze_cases.py                  # Analyze all cases
    python analyze_cases.py --from 300       # Analyze cases with id >= 300
    python analyze_cases.py --from 300 --to 320  # Analyze cases 300-320
    python analyze_cases.py --failed         # Only show failed cases
    python analyze_cases.py --csv            # Output as CSV

Reads Supabase credentials from .env in the same directory.
For cloud server runs/ disk usage, pass --ssh <host> (e.g. --ssh cfdqanda-prod).
"""

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

# Load .env from script directory
script_dir = Path(__file__).resolve().parent
env_path = script_dir / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            val = val.strip().strip('"').strip("'")
            os.environ.setdefault(key.strip(), val)

try:
    from supabase import create_client
except ImportError:
    print("ERROR: supabase package not installed. Run: pip install supabase")
    sys.exit(1)


def get_supabase():
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in .env")
        sys.exit(1)
    return create_client(url, key)


def get_user_map(sb):
    """Build user_id -> email mapping."""
    user_map = {}
    try:
        for u in sb.auth.admin.list_users():
            if hasattr(u, "id"):
                user_map[u.id] = u.email
    except Exception as e:
        print(f"WARNING: Could not fetch user list: {e}")
    return user_map


def get_disk_usage_ssh(ssh_host, foam_agent_dir="/home/apexflowcfd/Foam-Agent"):
    """Get per-run disk usage from remote server via SSH."""
    runs_dir = f"{foam_agent_dir}/runs"
    try:
        result = subprocess.run(
            ["ssh", "-o", "ConnectTimeout=10", ssh_host,
             f"for d in {runs_dir}/*/; do "
             f"run=$(basename $d); size=$(du -sm $d 2>/dev/null | cut -f1); "
             f"echo \"$run $size\"; done"],
            capture_output=True, text=True, timeout=60,
        )
        disk = {}
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2:
                try:
                    disk[int(parts[0])] = int(parts[1])
                except ValueError:
                    pass
        return disk
    except Exception as e:
        print(f"WARNING: SSH disk check failed: {e}")
        return {}


def get_local_disk_usage(foam_agent_dir):
    """Get per-run disk usage from local Foam-Agent/runs/."""
    runs_dir = Path(foam_agent_dir) / "runs"
    disk = {}
    if not runs_dir.is_dir():
        return disk
    for d in runs_dir.iterdir():
        if d.is_dir():
            try:
                run_id = int(d.name)
                # Use du for accuracy
                result = subprocess.run(
                    ["du", "-sm", str(d)], capture_output=True, text=True, timeout=30,
                )
                if result.returncode == 0:
                    disk[run_id] = int(result.stdout.split()[0])
            except (ValueError, Exception):
                pass
    return disk


def analyze(args):
    sb = get_supabase()
    user_map = get_user_map(sb)

    # Fetch simulations
    query = sb.table("simulations").select(
        "id, user_id, status, prompt, result_data, created_at, "
        "llm_config, pipeline_mode, pipeline_stage, user_rating, user_comment"
    )
    if args.from_id:
        query = query.gte("id", args.from_id)
    if args.to_id:
        query = query.lte("id", args.to_id)
    query = query.order("id", desc=False)
    resp = query.execute()
    rows = resp.data

    if not rows:
        print("No cases found.")
        return

    # Get disk usage
    disk = {}
    if args.ssh:
        print(f"Fetching disk usage from {args.ssh}...", file=sys.stderr)
        disk = get_disk_usage_ssh(args.ssh)
    elif args.local_foam_dir:
        disk = get_local_disk_usage(args.local_foam_dir)

    # Filter
    if args.failed:
        rows = [r for r in rows if r["status"] == "failed"]
    if args.status:
        rows = [r for r in rows if r["status"] == args.status]

    if not rows:
        print("No cases match the filter.")
        return

    # Build case records
    cases = []
    for row in rows:
        run_id = int(row["id"])
        lc = row.get("llm_config") or {}
        rd = row.get("result_data") or {}
        us = rd.get("upload_stats") or {}

        provider = lc.get("model_provider") or "openai-codex"
        model = lc.get("model_version") or "gpt-5.5"
        is_byok = bool(lc.get("api_key") or lc.get("model_provider"))

        error = rd.get("error", "")
        # Classify error
        error_type = ""
        if row["status"] == "failed":
            if "disk limit" in str(error).lower():
                error_type = "disk_exceeded"
            elif "timed out" in str(error).lower():
                error_type = "timeout"
            elif "return code -9" in str(error):
                error_type = "OOM (-9)"
            elif "return code" in str(error):
                code = str(error).split("return code")[-1].strip().rstrip(".")
                error_type = f"exit {code}"
            elif error:
                error_type = "other"
            else:
                error_type = "unknown"

        cases.append({
            "id": run_id,
            "user": user_map.get(row["user_id"], row["user_id"][:16]),
            "status": row["status"],
            "date": str(row.get("created_at") or "")[:16],
            "provider": provider,
            "model": model,
            "is_byok": is_byok,
            "mode": row.get("pipeline_mode") or "auto",
            "stage": row.get("pipeline_stage") or "",
            "error": str(error)[:80] if error else "",
            "error_type": error_type,
            "db_bytes": us.get("total_bytes", 0) or 0,
            "uploaded": us.get("uploaded", 0) or 0,
            "failed_uploads": us.get("failed", 0) or 0,
            "disk_mb": disk.get(run_id, 0),
            "rating": row.get("user_rating") or "",
            "comment": row.get("user_comment") or "",
            "prompt_short": str(row.get("prompt") or "")[:100],
            "prompt_full": str(row.get("prompt") or ""),
        })

    # Output
    if args.md:
        _output_md(cases, args, disk_available=bool(disk))
    elif args.csv:
        _output_csv(cases)
    else:
        _output_table(cases, disk_available=bool(disk))
        _output_summary(cases)


def _output_md(cases, args, disk_available=False):
    """Output as Markdown report."""
    from datetime import datetime

    out = sys.stdout
    if args.output:
        out = open(args.output, "w", encoding="utf-8")

    date_str = datetime.now().strftime("%Y-%m-%d")
    from_str = f"ID >= {args.from_id}" if args.from_id else "all"
    to_str = f" ~ {args.to_id}" if args.to_id else ""
    filter_str = f", filter={args.status or 'failed' if args.failed else 'all'}"

    out.write(f"# Case 分析报告\n\n")
    out.write(f"> 生成日期：{date_str}\n")
    out.write(f"> 范围：{from_str}{to_str}{filter_str}\n")
    out.write(f"> 生成命令：`python3 analyze_cases.py {' '.join(sys.argv[1:])}`\n\n")

    # Summary section
    total = len(cases)
    by_status = defaultdict(int)
    by_error = defaultdict(int)
    by_model = defaultdict(int)
    by_user = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})

    for c in cases:
        by_status[c["status"]] += 1
        if c["error_type"]:
            by_error[c["error_type"]] += 1
        by_model[f"{c['provider']}/{c['model']}"] += 1
        u = by_user[c["user"]]
        u["total"] += 1
        if c["status"] == "completed":
            u["completed"] += 1
        elif c["status"] == "failed":
            u["failed"] += 1

    completed = by_status.get("completed", 0)
    failed = by_status.get("failed", 0)
    finished = completed + failed
    rate = f"{completed}/{finished} = {completed/finished*100:.1f}%" if finished > 0 else "N/A"

    out.write(f"## 总览\n\n")
    out.write(f"- **总 case 数**：{total}\n")
    for status, count in sorted(by_status.items()):
        out.write(f"- **{status}**：{count} ({count/total*100:.0f}%)\n")
    out.write(f"- **成功率**：{rate}\n\n")

    if by_error:
        out.write(f"### 错误类型\n\n")
        out.write(f"| 类型 | 数量 |\n|------|------|\n")
        for err, count in sorted(by_error.items(), key=lambda x: -x[1]):
            out.write(f"| {err} | {count} |\n")
        out.write(f"\n")

    out.write(f"### 模型使用\n\n")
    out.write(f"| 模型 | 数量 |\n|------|------|\n")
    for model, count in sorted(by_model.items(), key=lambda x: -x[1]):
        out.write(f"| `{model}` | {count} |\n")
    out.write(f"\n")

    out.write(f"### 用户统计\n\n")
    out.write(f"| 用户 | 总数 | 成功 | 失败 | 成功率 |\n")
    out.write(f"|------|------|------|------|--------|\n")
    for user, stats in sorted(by_user.items(), key=lambda x: -x[1]["total"]):
        done = stats["completed"]
        fail = stats["failed"]
        r = f"{done/(done+fail)*100:.0f}%" if (done + fail) > 0 else "-"
        out.write(f"| {user} | {stats['total']} | {done} | {fail} | {r} |\n")
    out.write(f"\n---\n\n")

    # Per-case detail
    out.write(f"## 逐案详情\n\n")
    for c in cases:
        db_size = f"{c['db_bytes']/1024/1024:.1f} MB" if c["db_bytes"] > 0 else "-"
        disk_str = f"{c['disk_mb']} MB" if c["disk_mb"] > 0 else "-"
        byok = "BYOK" if c["is_byok"] else "平台默认"
        error_str = c["error"] if c["error"] else "-"

        out.write(f"### Run {c['id']} — {c['status']}")
        if disk_available and c["disk_mb"] > 0:
            out.write(f" — {c['disk_mb']} MB")
        out.write(f"\n\n")

        out.write(f"| 项目 | 详情 |\n|------|------|\n")
        out.write(f"| **用户** | {c['user']} |\n")
        out.write(f"| **日期** | {c['date']} |\n")
        out.write(f"| **模型** | `{c['provider']} / {c['model']}`（{byok}） |\n")
        out.write(f"| **模式** | {c['mode']} |\n")
        if c["stage"]:
            out.write(f"| **阶段** | {c['stage']} |\n")
        out.write(f"| **状态** | {c['status']} |\n")
        if c["error_type"]:
            out.write(f"| **错误类型** | {c['error_type']} |\n")
            out.write(f"| **错误信息** | {error_str} |\n")
        if disk_available and c["disk_mb"] > 0:
            out.write(f"| **磁盘占用** | {disk_str} |\n")
        if c["db_bytes"] > 0:
            out.write(f"| **DB 记录大小** | {db_size} |\n")
        if c["uploaded"]:
            out.write(f"| **上传文件数** | {c['uploaded']} (失败 {c['failed_uploads']}) |\n")
        if c["rating"]:
            out.write(f"| **用户评分** | {c['rating']} |\n")
        if c["comment"]:
            out.write(f"| **用户评论** | {c['comment']} |\n")

        # Prompt as blockquote (full text, preserve newlines)
        out.write(f"\n**Prompt：**\n\n")
        for line in c["prompt_full"].splitlines():
            out.write(f"> {line}\n")
        out.write(f"\n---\n\n")

    if args.output:
        out.close()
        print(f"Report written to {args.output}", file=sys.stderr)


def _output_csv(cases):
    """Output as CSV to stdout."""
    import csv

    fields = [k for k in cases[0].keys() if k != "prompt_full"]
    writer = csv.DictWriter(sys.stdout, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for c in cases:
        writer.writerow(c)


def _output_table(cases, disk_available=False):
    """Output formatted table."""
    print()
    if disk_available:
        header = f"{'ID':>5} {'Status':>10} {'Error':>12} {'Provider':>14} {'Model':>18} {'BYOK':>5} {'Mode':>10} {'Disk':>8} {'DB Size':>10} {'Date':>17} {'User':<30} Prompt"
    else:
        header = f"{'ID':>5} {'Status':>10} {'Error':>12} {'Provider':>14} {'Model':>18} {'BYOK':>5} {'Mode':>10} {'DB Size':>10} {'Date':>17} {'User':<30} Prompt"
    print("=" * len(header))
    print(header)
    print("=" * len(header))

    for c in cases:
        byok = "BYOK" if c["is_byok"] else ""
        db_size = f"{c['db_bytes']/1024/1024:.1f}MB" if c["db_bytes"] > 0 else "-"
        prompt = c["prompt_short"][:60]

        if disk_available:
            disk_str = f"{c['disk_mb']}MB" if c["disk_mb"] > 0 else "-"
            print(f"{c['id']:>5} {c['status']:>10} {c['error_type']:>12} {c['provider']:>14} {c['model']:>18} {byok:>5} {c['mode']:>10} {disk_str:>8} {db_size:>10} {c['date']:>17} {c['user']:<30} {prompt}")
        else:
            print(f"{c['id']:>5} {c['status']:>10} {c['error_type']:>12} {c['provider']:>14} {c['model']:>18} {byok:>5} {c['mode']:>10} {db_size:>10} {c['date']:>17} {c['user']:<30} {prompt}")


def _output_summary(cases):
    """Print summary statistics."""
    print()
    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)

    total = len(cases)
    by_status = defaultdict(int)
    by_error = defaultdict(int)
    by_model = defaultdict(int)
    by_user = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
    total_disk = 0
    total_db_bytes = 0

    for c in cases:
        by_status[c["status"]] += 1
        if c["error_type"]:
            by_error[c["error_type"]] += 1
        by_model[f"{c['provider']}/{c['model']}"] += 1
        u = by_user[c["user"]]
        u["total"] += 1
        if c["status"] == "completed":
            u["completed"] += 1
        elif c["status"] == "failed":
            u["failed"] += 1
        total_disk += c["disk_mb"]
        total_db_bytes += c["db_bytes"]

    # Status breakdown
    print(f"\nTotal cases: {total}")
    for status, count in sorted(by_status.items()):
        pct = count / total * 100
        print(f"  {status:<12} {count:>4} ({pct:.0f}%)")

    # Success rate
    completed = by_status.get("completed", 0)
    failed = by_status.get("failed", 0)
    finished = completed + failed
    if finished > 0:
        print(f"\nSuccess rate: {completed}/{finished} = {completed/finished*100:.1f}%")

    # Error breakdown
    if by_error:
        print(f"\nError types:")
        for err, count in sorted(by_error.items(), key=lambda x: -x[1]):
            print(f"  {err:<20} {count:>4}")

    # Model breakdown
    print(f"\nModels used:")
    for model, count in sorted(by_model.items(), key=lambda x: -x[1]):
        print(f"  {model:<40} {count:>4}")

    # Per-user stats
    print(f"\nPer-user:")
    print(f"  {'User':<35} {'Total':>5} {'Done':>5} {'Fail':>5} {'Rate':>6}")
    print(f"  {'-'*60}")
    for user, stats in sorted(by_user.items(), key=lambda x: -x[1]["total"]):
        done = stats["completed"]
        fail = stats["failed"]
        rate = f"{done/(done+fail)*100:.0f}%" if (done + fail) > 0 else "-"
        print(f"  {user:<35} {stats['total']:>5} {done:>5} {fail:>5} {rate:>6}")

    # Storage
    if total_disk > 0:
        print(f"\nDisk usage: {total_disk} MB ({total_disk/1024:.1f} GB)")
    if total_db_bytes > 0:
        print(f"DB recorded: {total_db_bytes/1024/1024:.1f} MB ({total_db_bytes/1024/1024/1024:.2f} GB)")


def main():
    parser = argparse.ArgumentParser(description="Analyze CFDQandA simulation cases")
    parser.add_argument("--from", dest="from_id", type=int, help="Start from case ID (inclusive)")
    parser.add_argument("--to", dest="to_id", type=int, help="End at case ID (inclusive)")
    parser.add_argument("--failed", action="store_true", help="Only show failed cases")
    parser.add_argument("--status", type=str, help="Filter by status (completed/failed/running/queued)")
    parser.add_argument("--csv", action="store_true", help="Output as CSV")
    parser.add_argument("--md", action="store_true", help="Output as Markdown report")
    parser.add_argument("-o", "--output", type=str, help="Write output to file (for --md/--csv)")
    parser.add_argument("--ssh", type=str, help="SSH host to check disk usage (e.g. cfdqanda-prod)")
    parser.add_argument("--local-foam-dir", type=str, help="Local Foam-Agent dir for disk usage check")
    args = parser.parse_args()
    analyze(args)


if __name__ == "__main__":
    main()
