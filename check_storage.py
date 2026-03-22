#!/usr/bin/env python3
"""
check_storage.py — Platform resource report (Markdown output).

Usage:
    python check_storage.py --ssh cfdqanda-prod              # Full report to stdout
    python check_storage.py --ssh cfdqanda-prod -o report.md  # Save to file
    python check_storage.py --quick --ssh cfdqanda-prod       # Skip Supabase
    python check_storage.py --detail --ssh cfdqanda-prod      # Per-task Supabase breakdown

Reads Supabase credentials from .env in the same directory.
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# Load .env
script_dir = Path(__file__).resolve().parent
env_path = script_dir / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def _fmt(bytes_val):
    if bytes_val >= 1024 * 1024 * 1024:
        return f"{bytes_val / 1024 / 1024 / 1024:.2f} GB"
    elif bytes_val >= 1024 * 1024:
        return f"{bytes_val / 1024 / 1024:.1f} MB"
    elif bytes_val >= 1024:
        return f"{bytes_val / 1024:.1f} KB"
    return f"{bytes_val} B"


def _run_cmd(cmd, ssh_host=None):
    if ssh_host:
        full = ["ssh", "-o", "ConnectTimeout=10", ssh_host, cmd]
    else:
        full = ["bash", "-c", cmd]
    try:
        r = subprocess.run(full, capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception as e:
        return ""


def _bar(used_pct):
    """Simple text progress bar."""
    filled = int(used_pct / 5)
    return f"[{'█' * filled}{'░' * (20 - filled)}] {used_pct:.0f}%"


# ============================================================
# Server check
# ============================================================

def check_server(p, label, ssh_host=None):
    run = lambda cmd: _run_cmd(cmd, ssh_host)

    p(f"\n## {label}\n")

    # --- Disk ---
    disk = run("df -h / | tail -1")
    if disk:
        parts = disk.split()
        if len(parts) >= 5:
            pct = int(parts[4].rstrip('%'))
            p(f"### Disk\n")
            p(f"{_bar(pct)}\n")
            p(f"| | |\n|---|---|\n"
              f"| Total | {parts[1]} |\n"
              f"| Used | {parts[2]} |\n"
              f"| Available | {parts[3]} |\n")

    # Breakdown (cloud only — local disk is huge so breakdown is less useful)
    if ssh_host:
        breakdown = run(
            "sudo du -sh /var/lib/docker /home /var/log /snap /opt /tmp 2>/dev/null | sort -rh"
        )
        if breakdown:
            p(f"\n**Disk breakdown:**\n")
            p(f"| Directory | Size |\n|---|---|\n")
            for line in breakdown.splitlines()[:6]:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    p(f"| `{parts[1]}` | {parts[0]} |\n")

    # WSL config
    if not ssh_host:
        wsl = run("cat /mnt/c/Users/*/.wslconfig 2>/dev/null")
        if wsl:
            mem_lines = [l for l in wsl.splitlines() if "memory" in l.lower()]
            if mem_lines:
                p(f"\n> WSL2 memory limit: **{mem_lines[0].split('=')[-1].strip()}**\n")

    # --- Docker ---
    docker_out = run("docker system df --format '{{.Type}}\t{{.Size}}\t{{.Reclaimable}}' 2>/dev/null")
    if docker_out:
        p(f"\n### Docker\n")
        p(f"| Type | Size | Reclaimable |\n|---|---|---|\n")
        for line in docker_out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                p(f"| {parts[0]} | {parts[1]} | {parts[2]} |\n")
            elif len(parts) >= 2:
                p(f"| {parts[0]} | {parts[1]} | — |\n")

    # Containers
    containers = run(
        "docker stats --no-stream --format "
        "'{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null"
    )
    if containers:
        p(f"\n**Running containers:**\n")
        p(f"| Container | CPU | Memory | Mem% |\n|---|---|---|---|\n")
        for line in containers.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                p(f"| `{parts[0]}` | {parts[1]} | {parts[2]} | {parts[3]} |\n")

    # --- Runs ---
    foam_dir = os.environ.get("FOAM_AGENT_DIR", "")
    if ssh_host:
        runs_out = run("du -sh /home/*/Foam-Agent/runs/ 2>/dev/null || du -sh /home/openfoam/Foam-Agent/runs/ 2>/dev/null")
        count = run("ls /home/*/Foam-Agent/runs/ 2>/dev/null | wc -l || ls /home/openfoam/Foam-Agent/runs/ 2>/dev/null | wc -l")
        big_runs = run(
            "for d in /home/*/Foam-Agent/runs/*/; do "
            "s=$(du -sm $d 2>/dev/null | cut -f1); "
            "[ \"$s\" -gt 10 ] 2>/dev/null && echo \"$s\t$(basename $d)\"; "
            "done | sort -rn | head -5"
        )
    elif foam_dir:
        runs_out = run(f"du -sh {foam_dir}/runs/ 2>/dev/null")
        count = run(f"ls {foam_dir}/runs/ 2>/dev/null | wc -l")
        big_runs = run(
            f"for d in {foam_dir}/runs/*/; do "
            f"s=$(du -sm $d 2>/dev/null | cut -f1); "
            f"[ \"$s\" -gt 10 ] 2>/dev/null && echo \"$s\t$(basename $d)\"; "
            f"done | sort -rn | head -5"
        )
    else:
        runs_out = count = big_runs = ""

    if runs_out:
        size = runs_out.split()[0] if runs_out.split() else "?"
        p(f"\n### Simulation Runs\n")
        p(f"- **Total size:** {size}\n")
        p(f"- **Run count:** {count.strip()}\n")
        p(f"- *These are local working directories. After upload to Supabase, they are auto-cleaned.*\n")
        if big_runs and big_runs.strip():
            p(f"\n| Run | Size |\n|---|---|\n")
            for line in big_runs.splitlines():
                parts = line.split("\t")
                if len(parts) == 2:
                    p(f"| {parts[1]} | {parts[0]} MB |\n")

    # --- Memory ---
    mem = run("free -h | grep Mem")
    swap = run("free -h | grep Swap")
    if mem:
        parts = mem.split()
        if len(parts) >= 4:
            total = parts[1]
            used = parts[2]
            avail = parts[-1]
            # Parse percentage
            try:
                t = float(total.rstrip('GiMi'))
                u = float(used.rstrip('GiMi'))
                pct = u / t * 100 if t > 0 else 0
            except (ValueError, ZeroDivisionError):
                pct = 0
            p(f"\n### Memory\n")
            p(f"{_bar(pct)}\n")
            p(f"| | |\n|---|---|\n"
              f"| Total | {total} |\n"
              f"| Used | {used} |\n"
              f"| Available | {avail} |\n")

    if swap:
        parts = swap.split()
        if len(parts) >= 3 and parts[1] != "0B":
            p(f"| Swap | {parts[1]} total, {parts[2]} used |\n")

    # --- Top processes ---
    top_procs = run(
        "ps aux --sort=-%mem | head -8 | tail -7 | "
        "awk '{printf \"%s\\t%s\\t%s\\t%s\\t%s\\n\", $1, $4, $3, int($6/1024), $11}'"
    )
    if top_procs:
        p(f"\n**Top processes (by memory):**\n")
        p(f"| User | Mem% | CPU% | RSS | Command |\n|---|---|---|---|---|\n")
        for line in top_procs.splitlines():
            parts = line.split("\t")
            if len(parts) >= 5:
                cmd = parts[4].split("/")[-1][:30]  # shorten command
                p(f"| {parts[0]} | {parts[1]}% | {parts[2]}% | {parts[3]} MB | `{cmd}` |\n")

    # --- Uptime ---
    uptime = run("uptime -p 2>/dev/null || uptime")
    if uptime:
        p(f"\n> Uptime: {uptime.strip()}\n")


# ============================================================
# Supabase
# ============================================================

def check_supabase(p, detail=False):
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    except Exception as e:
        p(f"\n## Supabase\n\nCould not connect: {e}\n")
        return

    user_map = {}
    try:
        for u in sb.auth.admin.list_users():
            if hasattr(u, "id"):
                user_map[u.id] = u.email
    except Exception:
        pass

    p(f"\n## Supabase\n")
    p(f"*Storage estimated from DB `upload_stats.total_bytes` — instant, no API scan needed.*\n")

    resp = sb.table("simulations").select("id, user_id, status, result_data").execute()

    user_stats = {}
    grand_total = 0

    for row in resp.data:
        uid = row["user_id"]
        rd = row.get("result_data") or {}
        us = rd.get("upload_stats") or {}
        tb = us.get("total_bytes", 0) or 0

        if uid not in user_stats:
            user_stats[uid] = {"email": user_map.get(uid, uid[:20]),
                               "total_bytes": 0, "tasks": 0, "task_list": []}
        user_stats[uid]["total_bytes"] += tb
        user_stats[uid]["tasks"] += 1
        if tb > 0:
            user_stats[uid]["task_list"].append({
                "id": row["id"], "status": row["status"],
                "bytes": tb, "uploaded": us.get("uploaded", 0),
            })
        grand_total += tb

    # Storage summary
    used_gb = grand_total / 1024 / 1024 / 1024
    used_pct = used_gb / 100 * 100

    p(f"\n### Storage (100 GB included with Pro)\n")
    p(f"{_bar(used_pct)}\n")
    p(f"| | |\n|---|---|\n"
      f"| Used | {used_gb:.2f} GB |\n"
      f"| Available | ~{100 - used_gb:.1f} GB |\n")

    p(f"\n### Per-User Storage\n")
    p(f"| User | Tasks | Storage |\n|---|---|---|\n")
    for uid in sorted(user_stats, key=lambda u: user_stats[u]["total_bytes"], reverse=True):
        s = user_stats[uid]
        if s["total_bytes"] == 0:
            continue
        p(f"| {s['email']} | {s['tasks']} | {_fmt(s['total_bytes'])} |\n")
    p(f"| **Total** | **{len(resp.data)}** | **{_fmt(grand_total)}** |\n")

    # Database
    p(f"\n### Database (8 GB included with Pro)\n")
    p(f"| | |\n|---|---|\n"
      f"| Simulations | {len(resp.data)} rows |\n"
      f"| Users | {len(user_map)} |\n")

    # Per-task detail
    if detail:
        p(f"\n### Per-Task Detail\n")
        p(f"| User | Task | Status | Files | Size |\n|---|---|---|---|---|\n")
        all_tasks = []
        for s in user_stats.values():
            for t in s["task_list"]:
                t["email"] = s["email"]
                all_tasks.append(t)
        for t in sorted(all_tasks, key=lambda x: x["bytes"], reverse=True):
            p(f"| {t['email']} | {t['id']} | {t['status']} | {t['uploaded']} | {_fmt(t['bytes'])} |\n")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="CFDQandA platform resource report")
    parser.add_argument("--detail", action="store_true", help="Per-task Supabase breakdown")
    parser.add_argument("--quick", action="store_true", help="Skip Supabase (servers only)")
    parser.add_argument("--ssh", type=str, action="append", default=[],
                        help="SSH host to check (e.g. --ssh cfdqanda-prod)")
    parser.add_argument("-o", "--output", type=str, help="Save report to .md file")
    args = parser.parse_args()

    lines = []

    def p(text):
        lines.append(text)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    p(f"# CFDQandA Platform Resource Report\n\n")
    p(f"> Generated: {now}\n")

    # Local server
    check_server(p, "Local Server (WSL2)")

    # Remote servers
    for host in args.ssh:
        check_server(p, f"Cloud Server ({host})", ssh_host=host)

    # Supabase
    if not args.quick:
        check_supabase(p, detail=args.detail)

    output = "".join(lines)

    # Print to terminal
    print(output)

    # Save to file
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\nReport saved to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
