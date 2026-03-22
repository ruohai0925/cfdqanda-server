#!/usr/bin/env python3
"""
weekly_review.py — CFDQandA 周报：诊断每个 case，找出改进方向

Usage:
    python3 weekly_review.py                              # 最近 7 天
    python3 weekly_review.py --days 3                     # 最近 3 天
    python3 weekly_review.py --since 2026-03-18           # 指定起始日期
    python3 weekly_review.py --ssh cfdqanda-prod          # 含服务器资源
    python3 weekly_review.py -o report.md                 # 保存到文件
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

script_dir = Path(__file__).resolve().parent
for line in (script_dir / ".env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"'))

from supabase import create_client
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _fmt(b):
    if b >= 1024**3: return f"{b/1024**3:.2f} GB"
    if b >= 1024**2: return f"{b/1024**2:.1f} MB"
    if b >= 1024: return f"{b/1024:.1f} KB"
    return f"{b} B"

def _bar(pct):
    filled = min(int(pct / 5), 20)
    return f"`[{'█' * filled}{'░' * (20 - filled)}]` **{pct:.0f}%**"

def _ssh(host, cmd):
    try:
        r = subprocess.run(["ssh", "-o", "ConnectTimeout=10", host, cmd],
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception:
        return ""

def _classify_error(rd):
    cat = rd.get("error_category") or ""
    error = str(rd.get("error", ""))
    if cat and cat not in ("uncategorized", "None", "null"):
        return cat
    if "timed out" in error.lower(): return "timeout"
    if "return code -9" in error or "return code 137" in error: return "oom"
    if "disk limit" in error.lower(): return "disk_exceeded"
    if "quota" in error.lower(): return "codex_quota"
    if error: return "agent_error"
    return "unknown"


def generate(args):
    L = []
    p = lambda t: L.append(t)

    # Time range
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    else:
        since = datetime.now(timezone.utc) - timedelta(days=args.days)
    since_str = since.strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Fetch data
    user_map = {}
    try:
        for u in sb.auth.admin.list_users():
            if hasattr(u, "id"):
                user_map[u.id] = u.email
    except Exception:
        pass

    profiles = {}
    try:
        for row in sb.table("user_profiles").select("*").execute().data:
            profiles[row["id"]] = row
    except Exception:
        pass

    tasks = sb.table("simulations").select("*").gte(
        "created_at", since.isoformat()
    ).order("created_at", desc=False).execute().data

    if not tasks:
        p("**该时间段内没有任务。**\n")
        return "".join(L)

    # Classify
    completed = [t for t in tasks if t["status"] == "completed"]
    failed = [t for t in tasks if t["status"] == "failed"]
    running = [t for t in tasks if t["status"] in ("running", "queued")]
    cancelled = [t for t in tasks if t["status"] == "cancelled"]
    finished = completed + failed
    rate = len(completed) / len(finished) * 100 if finished else 0

    p(f"# CFDQandA 周报\n\n")
    p(f"> {since_str} ~ {now_str}｜{len(tasks)} 个任务｜成功率 **{rate:.0f}%** ({len(completed)}/{len(finished)})\n\n")

    # ============================================================
    # 1. 失败诊断（核心）
    # ============================================================
    if failed:
        p(f"## 失败诊断（{len(failed)} 个）\n\n")

        error_cats = Counter(_classify_error(t.get("result_data") or {}) for t in failed)
        p(f"错误分布：")
        p(f" | ".join(f"**{cat}** {n}" for cat, n in error_cats.most_common()))
        p(f"\n\n")

        for t in failed:
            rd = t.get("result_data") or {}
            cat = _classify_error(rd)
            email = user_map.get(t["user_id"], "?")
            prof = profiles.get(t["user_id"], {})
            org = prof.get("organization") or ""
            error_msg = str(rd.get("error", ""))
            prompt = str(t.get("prompt") or "")

            p(f"### Task {t['id']} — {cat}\n\n")
            p(f"**{email}**")
            if org:
                p(f" ({org})")
            p(f" · {str(t.get('created_at',''))[:16]}\n\n")

            # Error detail
            if error_msg:
                p(f"**错误：** {error_msg}\n\n")

            # Full prompt
            p(f"**Prompt：**\n\n")
            for line in prompt.splitlines():
                p(f"> {line}\n")
            p(f"\n")

            # Allrun audit issues
            aa = rd.get("allrun_audit") or {}
            if aa.get("results"):
                for r in aa["results"]:
                    for dc in r.get("dangerous_commands", []):
                        p(f"**安全警告：** 行 {dc.get('line_num')}: `{dc.get('line')}`\n\n")

            # Stage feedback (if controlled mode)
            sf = rd.get("stage_feedback") or []
            if sf:
                p(f"**阶段反馈：**\n")
                for fb in sf:
                    action = fb.get("action", "?")
                    stage = fb.get("stage", "?")
                    comment = fb.get("comment", "")
                    p(f"- {stage}: {action}")
                    if comment:
                        p(f" — {comment}")
                    p(f"\n")
                p(f"\n")

            p(f"---\n\n")

    # ============================================================
    # 2. 成功案例
    # ============================================================
    if completed:
        p(f"## 成功案例（{len(completed)} 个）\n\n")
        p(f"| ID | 用户 | 输出 | Prompt |\n|---|---|---|---|\n")
        for t in completed:
            email = user_map.get(t["user_id"], "?")
            rd = t.get("result_data") or {}
            us = rd.get("upload_stats") or {}
            size = _fmt(us.get("total_bytes", 0) or 0)
            prompt = str(t.get("prompt") or "")[:70].replace("\n", " ").replace("|", "/")
            p(f"| {t['id']} | {email} | {size} | {prompt} |\n")

        # Token usage summary (concise)
        token_total = sum(
            ((t.get("result_data") or {}).get("token_usage") or {}).get("total_tokens", 0)
            for t in completed
        )
        if token_total:
            p(f"\n总 token 消耗：{token_total:,}\n")

    # ============================================================
    # 3. 用户反馈
    # ============================================================
    rated = [t for t in tasks if t.get("user_rating")]
    commented = [t for t in tasks if t.get("user_comment")]
    stage_fbs = []
    for t in tasks:
        rd = t.get("result_data") or {}
        for fb in (rd.get("stage_feedback") or []):
            if fb.get("comment"):
                stage_fbs.append({"id": t["id"], "user": user_map.get(t["user_id"], "?"), **fb})

    if rated or commented or stage_fbs:
        p(f"\n## 用户反馈\n\n")
        rating_labels = {1: "成功 ✅", 2: "部分成功 ⚠️", 3: "失败 ❌"}
        for t in rated:
            email = user_map.get(t["user_id"], "?")
            label = rating_labels.get(t["user_rating"], "?")
            p(f"- **Task {t['id']}** ({email}): {label}")
            if t.get("user_comment"):
                p(f" — {t['user_comment']}")
            p(f"\n")
        for fb in stage_fbs:
            p(f"- **Task {fb['id']}** ({fb['user']}): {fb.get('stage','?')} {fb.get('action','?')} — {fb['comment']}\n")
    else:
        p(f"\n## 用户反馈\n\n该时间段内无评分或反馈。\n")

    # ============================================================
    # 4. 用户概览
    # ============================================================
    p(f"\n## 用户概览\n\n")

    user_stats = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
    for t in tasks:
        uid = t["user_id"]
        user_stats[uid]["total"] += 1
        if t["status"] == "completed":
            user_stats[uid]["completed"] += 1
        elif t["status"] == "failed":
            user_stats[uid]["failed"] += 1

    p(f"| 用户 | 机构 | 任务 | 成功 | 失败 | 率 |\n|---|---|---|---|---|---|\n")
    for uid in sorted(user_stats, key=lambda u: user_stats[u]["total"], reverse=True):
        s = user_stats[uid]
        email = user_map.get(uid, uid[:16])
        prof = profiles.get(uid, {})
        org = prof.get("organization") or "-"
        done, fail = s["completed"], s["failed"]
        r = f"{done/(done+fail)*100:.0f}%" if (done + fail) > 0 else "-"
        p(f"| {email} | {org} | {s['total']} | {done} | {fail} | {r} |\n")

    # ============================================================
    # 5. 仍在运行/排队的任务
    # ============================================================
    if running:
        p(f"\n## 仍在运行（{len(running)} 个）\n\n")
        for t in running:
            email = user_map.get(t["user_id"], "?")
            prompt = str(t.get("prompt") or "")[:80].replace("\n", " ")
            elapsed = ""
            try:
                created = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
                hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
                elapsed = f"已运行 {hours:.0f}h"
            except Exception:
                pass
            p(f"- **Task {t['id']}** ({email}) {elapsed}: {prompt}\n")

    # ============================================================
    # 6. 服务器 & 存储（简要）
    # ============================================================
    if args.ssh:
        p(f"\n## 服务器状态\n\n")
        for host in args.ssh:
            disk = _ssh(host, "df -h / | tail -1")
            mem = _ssh(host, "free -h | grep Mem")
            if disk:
                parts = disk.split()
                if len(parts) >= 5:
                    p(f"**{host}** 磁盘 {_bar(int(parts[4].rstrip('%')))} ({parts[3]} 可用)")
            if mem:
                parts = mem.split()
                if len(parts) >= 4:
                    p(f"，内存 {parts[2]}/{parts[1]}")
            p(f"\n\n")

    # Supabase (one line)
    total_bytes = sum(
        ((r.get("result_data") or {}).get("upload_stats") or {}).get("total_bytes", 0) or 0
        for r in sb.table("simulations").select("id, result_data").execute().data
    )
    p(f"**Supabase** {_bar(total_bytes/1024**3/100*100)} ({_fmt(total_bytes)} / 100 GB)\n")

    # ============================================================
    # 7. 改进建议
    # ============================================================
    p(f"\n## 改进建议\n\n")

    if rate < 50:
        p(f"- ⚠️ 成功率 {rate:.0f}%，低于 50% 目标。需要重点分析失败 case 的 simulation.log。\n")
    elif rate < 80:
        p(f"- 📊 成功率 {rate:.0f}%，有改善空间。\n")
    else:
        p(f"- ✅ 成功率 {rate:.0f}%，表现良好。\n")

    if failed:
        cats = Counter(_classify_error(t.get("result_data") or {}) for t in failed)
        top_cat, top_n = cats.most_common(1)[0]
        notes = {
            "timeout": "建议引导用户延长超时或放宽收敛标准",
            "oom": "网格太大，考虑限制 snappyHexMesh 加密级别",
            "agent_error": "需要逐个查看 simulation.log 定位 Foam-Agent 的具体错误",
            "disk_exceeded": "writeInterval 过密，Foam-Agent 应使用 purgeWrite",
        }
        p(f"- 最常见失败：**{top_cat}**（{top_n} 个）。{notes.get(top_cat, '')}\n")

    if not rated:
        p(f"- 📋 无用户评分。建议直接联系活跃用户收集反馈。\n")

    # Stuck jobs
    for t in running:
        try:
            created = datetime.fromisoformat(t["created_at"].replace("Z", "+00:00"))
            hours = (datetime.now(timezone.utc) - created).total_seconds() / 3600
            if hours > 24:
                p(f"- ⚠️ Task {t['id']} 已运行 {hours:.0f} 小时，可能卡住了（stale job）。\n")
        except Exception:
            pass

    p(f"\n---\n*`weekly_review.py` · {now_str}*\n")
    return "".join(L)


def main():
    parser = argparse.ArgumentParser(description="CFDQandA 周报")
    parser.add_argument("--days", type=int, default=7, help="最近 N 天（默认 7）")
    parser.add_argument("--since", type=str, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--ssh", type=str, action="append", default=[], help="SSH 服务器")
    parser.add_argument("-o", "--output", type=str, help="保存到文件")
    args = parser.parse_args()

    report = generate(args)
    print(report)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\n报告已保存到 {args.output}", file=sys.stderr)

if __name__ == "__main__":
    main()
