#!/usr/bin/env python3
"""
check_storage.py — CFDQandA 平台资源报告（Markdown 格式）

用法：
    python check_storage.py --ssh cfdqanda-prod              # 完整报告
    python check_storage.py --ssh cfdqanda-prod -o report.md  # 保存到文件
    python check_storage.py --quick --ssh cfdqanda-prod       # 跳过 Supabase
    python check_storage.py --detail --ssh cfdqanda-prod      # 含每个任务的 Supabase 明细

从同目录下的 .env 文件读取 Supabase 凭据。
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
    except Exception:
        return ""


def _bar(used_pct):
    filled = int(used_pct / 5)
    return f"`[{'█' * filled}{'░' * (20 - filled)}]` **{used_pct:.0f}%**"


# ============================================================
# 服务器检查
# ============================================================

def check_server(p, label, ssh_host=None):
    run = lambda cmd: _run_cmd(cmd, ssh_host)

    p(f"\n## {label}\n")

    # --- 磁盘 ---
    disk = run("df -h / | tail -1")
    if disk:
        parts = disk.split()
        if len(parts) >= 5:
            pct = int(parts[4].rstrip('%'))
            p(f"### 磁盘\n\n")
            p(f"服务器根分区（`/`）的整体使用情况：\n\n")
            p(f"{_bar(pct)}\n\n")
            p(f"| 项目 | 值 |\n|---|---|\n"
              f"| 总容量 | {parts[1]} |\n"
              f"| 已使用 | {parts[2]} |\n"
              f"| 剩余可用 | {parts[3]} |\n")

    # 磁盘分项（云端才显示，本地 1TB 不需要细看）
    if ssh_host:
        breakdown = run(
            "sudo du -sh /var/lib/docker /home /var/log /snap /opt /tmp 2>/dev/null | sort -rh"
        )
        if breakdown:
            p(f"\n磁盘中各主要目录的占用（包含在上面的「已使用」中）：\n\n")
            p(f"| 目录 | 大小 | 说明 |\n|---|---|---|\n")
            notes = {
                "/var/lib/docker": "Docker 镜像 + 容器数据",
                "/home": "用户目录（含 Foam-Agent、runs/）",
                "/var/log": "系统日志",
                "/snap": "Snap 包",
                "/opt": "第三方软件",
                "/tmp": "临时文件",
            }
            for line in breakdown.splitlines()[:6]:
                parts = line.split(None, 1)
                if len(parts) == 2:
                    note = notes.get(parts[1], "")
                    p(f"| `{parts[1]}` | {parts[0]} | {note} |\n")

    # WSL 配置（仅本地）
    if not ssh_host:
        wsl = run("cat /mnt/c/Users/*/.wslconfig 2>/dev/null")
        if wsl:
            mem_lines = [l for l in wsl.splitlines() if "memory" in l.lower()]
            if mem_lines:
                limit = mem_lines[0].split('=')[-1].strip()
                p(f"\n> WSL2 分配给 Linux 的内存上限：**{limit}**"
                  f"（剩余归 Windows 使用，影响 VS Code / 浏览器流畅度）\n")

    # --- Docker ---
    docker_out = run("docker system df --format '{{.Type}}\t{{.Size}}\t{{.Reclaimable}}' 2>/dev/null")
    if docker_out:
        p(f"\n### Docker\n\n")
        p(f"Docker 占用的磁盘空间（包含在上面磁盘的 `/var/lib/docker` 中）：\n\n")
        p(f"| 类型 | 大小 | 可回收 | 说明 |\n|---|---|---|---|\n")
        type_notes = {
            "Images": "镜像文件（cfdqanda-worker ~28.5GB, cfdqanda-api ~300MB）",
            "Containers": "容器运行时产生的数据（日志、临时文件）",
            "Local Volumes": "持久化数据卷",
            "Build Cache": "构建缓存（`docker builder prune` 可清理）",
        }
        for line in docker_out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                note = type_notes.get(parts[0], "")
                p(f"| {parts[0]} | {parts[1]} | {parts[2]} | {note} |\n")

    # 运行中的容器
    containers = run(
        "docker stats --no-stream --format "
        "'{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}' 2>/dev/null"
    )
    if containers:
        p(f"\n当前运行的 Docker 容器（实时 CPU 和内存占用）：\n\n")
        p(f"| 容器 | CPU | 内存用量 | 内存% | 说明 |\n|---|---|---|---|---|\n")
        container_notes = {
            "cfdqanda-api": "API 服务器",
            "cfdqanda-server-worker-1": "Worker 1（含 MCP Server ~3GB）",
            "cfdqanda-server-worker-2": "Worker 2（含 MCP Server ~3GB）",
        }
        for line in containers.splitlines():
            parts = line.split("\t")
            if len(parts) >= 4:
                name = parts[0]
                note = container_notes.get(name, "")
                p(f"| `{name}` | {parts[1]} | {parts[2]} | {parts[3]} | {note} |\n")

    # --- 仿真运行目录 ---
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
        p(f"\n### 仿真运行目录 (`runs/`)\n\n")
        p(f"Foam-Agent 仿真任务的本地工作目录。任务完成/失败后文件会上传到 Supabase Storage，"
          f"然后自动删除本地副本。如果看到大文件残留，说明是正在运行的任务或清理失败。\n\n")
        p(f"- **总大小：** {size}\n")
        p(f"- **目录数：** {count.strip()}\n")
        if big_runs and big_runs.strip():
            p(f"\n超过 10MB 的运行目录：\n\n")
            p(f"| Run ID | 大小 |\n|---|---|\n")
            for line in big_runs.splitlines():
                parts = line.split("\t")
                if len(parts) == 2:
                    p(f"| {parts[1]} | {parts[0]} MB |\n")

    # --- 内存 ---
    mem = run("free -h | grep Mem")
    swap = run("free -h | grep Swap")
    if mem:
        parts = mem.split()
        if len(parts) >= 4:
            total = parts[1]
            used = parts[2]
            avail = parts[-1]
            try:
                t = float(total.rstrip('GiMi'))
                u = float(used.rstrip('GiMi'))
                pct = u / t * 100 if t > 0 else 0
            except (ValueError, ZeroDivisionError):
                pct = 0
            p(f"\n### 内存\n\n")
            p(f"物理内存使用情况（Docker 容器的内存包含在内）：\n\n")
            p(f"{_bar(pct)}\n\n")
            p(f"| 项目 | 值 |\n|---|---|\n"
              f"| 总量 | {total} |\n"
              f"| 已使用 | {used} |\n"
              f"| 可用 | {avail} |\n")

    if swap:
        parts = swap.split()
        if len(parts) >= 3 and parts[1] != "0B":
            p(f"| 交换分区 | {parts[1]} 总量, {parts[2]} 已使用 |\n")

    # --- 进程 ---
    top_procs = run(
        "ps aux --sort=-%mem | head -8 | tail -7 | "
        "awk '{printf \"%s\\t%s\\t%s\\t%s\\t%s\\n\", $1, $4, $3, int($6/1024), $11}'"
    )
    if top_procs:
        p(f"\n内存占用最高的进程（与上面「已使用」内存对应）：\n\n")
        p(f"| 用户 | 内存% | CPU% | RSS (MB) | 进程 |\n|---|---|---|---|---|\n")
        for line in top_procs.splitlines():
            parts = line.split("\t")
            if len(parts) >= 5:
                cmd = parts[4].split("/")[-1][:30]
                p(f"| {parts[0]} | {parts[1]}% | {parts[2]}% | {parts[3]} | `{cmd}` |\n")

    # --- 运行时间 ---
    uptime = run("uptime -p 2>/dev/null || uptime")
    if uptime:
        p(f"\n> 已运行：{uptime.strip()}\n")


# ============================================================
# Supabase
# ============================================================

def check_supabase(p, detail=False):
    try:
        from supabase import create_client
        sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])
    except Exception as e:
        p(f"\n## Supabase\n\n无法连接：{e}\n")
        return

    # User map — paginated. Default per_page=50 was missing emails for >100 users.
    user_map = {}
    page = 1
    while True:
        try:
            batch = sb.auth.admin.list_users(page=page, per_page=200)
        except Exception:
            break
        if not batch:
            break
        for u in batch:
            if hasattr(u, "id"):
                user_map[u.id] = u.email
        if len(batch) < 200:
            break
        page += 1

    p(f"\n## Supabase（云端数据库 + 文件存储）\n\n")
    p(f"Supabase 是平台的云端后端，提供三项服务：\n")
    p(f"- **数据库（PostgreSQL）**：存储任务记录（`simulations` 表）、用户信息等\n")
    p(f"- **文件存储（Storage）**：存储仿真结果文件（配置文件、日志、时间步数据），用户在前端浏览/下载的文件来自这里\n")
    p(f"- **认证（Auth）**：用户注册登录\n\n")
    p(f"*下方存储用量直接遍历 `simulation_results` bucket 计算，是真值。*\n")

    # Walk Storage directly — DB-derived totals miss orphan dirs that survived
    # TTL purge of their DB rows (e.g. before storage_base_path was added,
    # or after the 2026-04-09 incident).
    bucket = sb.storage.from_("simulation_results")
    user_storage = {}     # uid -> {'bytes': int, 'task_ids': set, 'tasks': {tid: bytes}}
    grand_total = 0
    grand_files = 0

    def walk(prefix, uid=None, tid=None, depth=0):
        nonlocal grand_total, grand_files
        if depth > 5:
            return
        try:
            items = bucket.list(prefix, options={'limit': 1000})
        except Exception:
            return
        for it in items:
            n = it.get('name')
            if not n:
                continue
            full = f"{prefix}/{n}"
            if it.get('id') is None:
                # depth tracks current listing level; n is the *subdir* name.
                # depth=0 → currently listing 'public/' → n is a uid
                # depth=1 → currently listing 'public/{uid}/' → n is a tid
                if depth == 0:
                    walk(full, n, None, depth + 1)
                elif depth == 1:
                    walk(full, uid, n, depth + 1)
                else:
                    walk(full, uid, tid, depth + 1)
            else:
                sz = (it.get('metadata') or {}).get('size', 0) or 0
                grand_total += sz
                grand_files += 1
                if uid:
                    s = user_storage.setdefault(uid, {'bytes': 0, 'task_ids': set(), 'tasks': {}})
                    s['bytes'] += sz
                    if tid:
                        s['task_ids'].add(tid)
                        s['tasks'][tid] = s['tasks'].get(tid, 0) + sz

    walk("public", depth=0)

    # Storage usage
    used_gb = grand_total / 1024 / 1024 / 1024
    used_pct = used_gb / 100 * 100
    p(f"\n### 文件存储（Pro 计划含 100 GB）\n\n")
    p("所有仿真任务完成/失败后，输出文件会上传到 Supabase Storage 的 `simulation_results` 存储桶。"
      "用户在前端看到的「浏览文件」和「下载 ZIP」都从这里读取。\n\n")
    p(f"{_bar(used_pct)}\n\n")
    p(f"| 项目 | 值 |\n|---|---|\n"
      f"| 已使用 | {_fmt(grand_total)} ({grand_files} 文件) |\n"
      f"| 剩余可用 | ~{100 - used_gb:.1f} GB |\n")

    # Cross-reference DB row statuses where available (for the per-user table)
    db_rows = []
    try:
        db_rows = sb.table('simulations').select('id, user_id, status').execute().data
    except Exception:
        pass
    db_status = {row['id']: row['status'] for row in db_rows}

    # Per-user from Storage
    p(f"\n### 每用户存储用量\n\n")
    p(f"每个用户上传到 Supabase Storage 的文件总大小（直接从 bucket 统计，非 DB 估算）：\n\n")
    p(f"| 用户 | 任务数 | 存储用量 |\n|---|---|---|\n")
    for uid in sorted(user_storage, key=lambda u: -user_storage[u]['bytes']):
        s = user_storage[uid]
        if s['bytes'] == 0:
            continue
        email = user_map.get(uid, uid[:8])
        p(f"| {email} | {len(s['task_ids'])} | {_fmt(s['bytes'])} |\n")
    p(f"| **合计** | **{sum(len(s['task_ids']) for s in user_storage.values())}** "
      f"| **{_fmt(grand_total)}** |\n")

    # Database
    p(f"\n### 数据库（Pro 计划含 8 GB）\n\n")
    p(f"PostgreSQL 数据库存储任务元数据（prompt、状态、结果摘要等），不含实际仿真文件。\n\n")
    p(f"| 项目 | 值 |\n|---|---|\n"
      f"| 仿真任务数 | {len(db_rows)} 行 |\n"
      f"| 注册用户数 | {len(user_map)} |\n")
    if grand_files > 0 and len(db_rows) == 0:
        p(f"\n> ⚠️ Storage 有 {grand_files} 个文件但 DB 0 行——是不是 TTL 把行清了？\n")

    # 每任务明细
    if detail:
        p(f"\n### 每任务存储明细\n\n")
        p(f"| 用户 | 任务 ID | 状态 | 存储大小 |\n|---|---|---|---|\n")
        flat = []
        for uid, s in user_storage.items():
            email = user_map.get(uid, uid[:8])
            for tid, sz in s['tasks'].items():
                flat.append((email, tid, sz))
        for email, tid, sz in sorted(flat, key=lambda x: -x[2]):
            tid_int = int(tid) if tid.isdigit() else None
            status = db_status.get(tid_int, "(no DB row)")
            p(f"| {email} | {tid} | {status} | {_fmt(sz)} |\n")


# ============================================================
# 主入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="CFDQandA 平台资源报告")
    parser.add_argument("--detail", action="store_true", help="显示 Supabase 每任务存储明细")
    parser.add_argument("--quick", action="store_true", help="跳过 Supabase（只检查服务器）")
    parser.add_argument("--ssh", type=str, action="append", default=[],
                        help="要检查的远程服务器（如 --ssh cfdqanda-prod）")
    parser.add_argument("-o", "--output", type=str, help="保存报告到 .md 文件")
    args = parser.parse_args()

    lines = []

    def p(text):
        lines.append(text)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    p(f"# CFDQandA 平台资源报告\n\n")
    p(f"> 生成时间：{now}\n\n")
    p(f"本报告检查三类资源的使用情况：\n")
    p(f"1. **本地服务器（WSL2）**：开发机，运行 Docker 容器（API + Worker），提供本地 Worker 算力\n")
    p(f"2. **云端服务器**：GCP 虚拟机，运行生产环境的 API + Worker\n")
    p(f"3. **Supabase**：云端数据库 + 文件存储，所有仿真结果的最终存储位置\n\n")
    p(f"磁盘空间的包含关系：服务器磁盘 ⊃ Docker (镜像+容器+缓存) + `runs/`(临时工作目录) + 系统文件。"
      f"仿真文件的最终归宿是 Supabase Storage，服务器磁盘只是临时中转。\n")

    # 本地服务器
    check_server(p, "本地服务器（WSL2）")

    # 远程服务器
    for host in args.ssh:
        check_server(p, f"云端服务器（{host}）", ssh_host=host)

    # Supabase
    if not args.quick:
        check_supabase(p, detail=args.detail)

    output = "".join(lines)
    print(output)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"\n报告已保存到 {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
