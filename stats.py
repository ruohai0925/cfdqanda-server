#!/usr/bin/env python3
"""stats.py — platform usage report (users, affiliations, volume, CFD domains).

One command for the numbers that get asked for repeatedly: how many users, from
which institutions, how much was run, in what areas.

Sources, in order of reliability:
  * auth.users / user_profiles  — never purged, so user counts are exact
  * simulation_archive          — permanent per-task record (once applied)
  * simulations                 — live rows only; TTL keeps ~3 months and
                                  expires failures faster than successes, so
                                  success rates computed from it read high
  * Storage + max(id)           — reconstructs history predating the archive

Usage:
    python3 stats.py                    # full report
    python3 stats.py --no-storage       # skip the (slow) Storage scan
    python3 stats.py --json out.json    # machine-readable dump
"""

import argparse
import collections
import datetime
import json
import os
import re
import sys

import taxonomy
from supabase_rest import Supa

BUCKET = "simulation_results"

# Self-reported organisations arrive as "SJTU", "上海交通大学", "Shanghai Jiao
# Tong University" — all one institution. Merge the common ones before counting.
ALIAS = [
    (r'sjtu|上海交通|shanghai jiao', '上海交通大学 SJTU'),
    (r'\bthu\b|tsinghua|清华', '清华大学 Tsinghua'),
    (r'\bpku\b|peking|北京大学', '北京大学 PKU'),
    (r'\bzju\b|zhejiang univ|浙江大学', '浙江大学 ZJU'),
    (r'\bustc\b|science and technology of china|中国科学技术大学|中科大', '中国科学技术大学 USTC'),
    (r'\bhit\b|harbin institute|哈尔滨工业|哈工大', '哈尔滨工业大学 HIT'),
    (r'\bdut\b|dalian university of tech|大连理工', '大连理工大学 DUT'),
    (r'nwpu|northwestern polytech|西北工业', '西北工业大学 NWPU'),
    (r'\bxjtu\b|xi.?an jiaotong|西安交通', '西安交通大学 XJTU'),
    (r'hust|huazhong|华中科技', '华中科技大学 HUST'),
    (r'buaa|beihang|北京航空航天', '北京航空航天大学 BUAA'),
    (r'\brpi\b|rensselaer', 'Rensselaer Polytechnic Institute'),
    (r'china university of petroleum|中国石油大学', '中国石油大学 CUP'),
    (r'重庆大学|chongqing univ', '重庆大学 CQU'),
    (r'东京大学|university of tokyo', '东京大学 UTokyo'),
    (r'中国科学院|chinese academy of sciences', '中国科学院 CAS'),
    (r'天津大学|tianjin univ', '天津大学 TJU'),
    (r'四川大学|sichuan univ', '四川大学 SCU'),
    (r'同济|tongji', '同济大学 Tongji'),
    (r'东南大学|southeast univ', '东南大学 SEU'),
    (r'华南理工|south china university of tech', '华南理工大学 SCUT'),
    (r'内蒙古工业', '内蒙古工业大学'),
]
NON_ANSWER = re.compile(r'^(无|没有|个人|personal|none|na|n/a|null|test|测试|-+|\.+|'
                        r'自由职业|无单位|私人|自己|123|x+|wu|w|u)$', re.I)
ACADEMIC = re.compile(r'大学|学院|学校|university|univ|college|institute|academy|研究院|'
                      r'研究所|school|laborat|实验室|\.edu|campus|polytech|理工', re.I)
COMPANY = re.compile(r'有限|公司|科技|集团|corp|\binc\b|ltd|llc|gmbh|company|technolog|'
                     r'energy|motors|engineering co|股份|电力|能源|研发中心', re.I)


def canon_org(org):
    low = re.sub(r'\s+', ' ', org.strip().lower())
    for pat, name in ALIAS:
        if re.search(pat, low):
            return name
    return org.strip()


def classify_org(org):
    if ACADEMIC.search(org) or any(re.search(p, org.lower()) for p, _ in ALIAS):
        return 'academic'
    if COMPANY.search(org):
        return 'company'
    return 'unclassified'


def month(ts):
    return (ts or '')[:7]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-storage", action="store_true")
    ap.add_argument("--json", metavar="PATH")
    args = ap.parse_args()
    sb = Supa()
    out = {}

    # ---------- users ----------
    users = sb.auth_users()
    profiles = sb.select("user_profiles", "select=id,organization,created_at")
    emails = {u["id"]: (u.get("email") or "") for u in users}
    signups = collections.Counter(month(u["created_at"]) for u in users)

    def days_between(a, b):
        f = "%Y-%m-%dT%H:%M:%S"
        return (datetime.datetime.strptime(a[:19], f) - datetime.datetime.strptime(b[:19], f)).days
    gaps = [days_between(u["last_sign_in_at"], u["created_at"])
            for u in users if u.get("last_sign_in_at")]

    out["users"] = {
        "registered": len(users),
        "confirmed": sum(1 for u in users if u.get("email_confirmed_at")),
        "signups_by_month": dict(sorted(signups.items())),
        "returned_after_day_one": sum(1 for g in gaps if g >= 1),
        "returned_after_30_days": sum(1 for g in gaps if g >= 30),
    }

    orgs = [(p.get("organization") or "").strip() for p in profiles]
    real = [o for o in orgs if o and not NON_ANSWER.match(o)]
    canoned = [canon_org(o) for o in real]
    buckets = collections.Counter(classify_org(o) for o in canoned)
    out["affiliations"] = {
        "profiles": len(profiles),
        "filled": sum(1 for o in orgs if o),
        "usable": len(real),
        "distinct_institutions": len(set(canoned)),
        "by_type": dict(buckets),
        "top": collections.Counter(canoned).most_common(20),
    }

    # ---------- volume ----------
    live = sb.select("simulations", "select=id,user_id,created_at,status,prompt,"
                                    "llm_config,user_rating&order=id")
    archived = sb.select("simulation_archive", "select=*&order=id") \
        if sb.table_exists("simulation_archive") else []
    out["archive_rows"] = len(archived)

    tasks = {}   # id -> unified record, archive first, live wins
    for a in archived:
        tasks[a["id"]] = dict(a)
    for r in live:
        shape = taxonomy.summarize(r.get("prompt"))
        tasks[r["id"]] = {**tasks.get(r["id"], {}), "id": r["id"], "user_id": r.get("user_id"),
                          "created_at": r.get("created_at"), "status": r.get("status"),
                          "user_rating": r.get("user_rating"), **shape}

    storage_ids = {}
    if not args.no_storage:
        for u in sb.storage_list(BUCKET, "public"):
            for j in sb.storage_list(BUCKET, f"public/{u['name']}"):
                if j["name"].isdigit():
                    storage_ids[int(j["name"])] = u["name"]
        for jid, uid in storage_ids.items():
            tasks.setdefault(jid, {"id": jid, "user_id": uid, "created_at": None,
                                   "status": None, "domain_tags": [],
                                   "is_platform_test": False})

    max_id = max([t["id"] for t in tasks.values()] or [0])
    out["volume"] = {
        "ever_submitted_upper_bound": max_id,
        "evidence_rows": len(tasks),
        "live_rows": len(live),
        "storage_folders": len(storage_ids),
        "by_month": dict(sorted(collections.Counter(
            month(t["created_at"]) for t in tasks.values() if t.get("created_at")).items())),
    }

    # ---------- domains ----------
    # Accounts whose submissions are platform smoke tests, not usage. Set
    # PLATFORM_TEST_EMAILS in .env (comma-separated) or they all count as users.
    owner = {e.strip().lower()
             for e in os.environ.get("PLATFORM_TEST_EMAILS", "").split(",") if e.strip()}
    def is_ours(t):
        return t.get("is_platform_test") or emails.get(t.get("user_id"), "").lower() in owner
    user_tasks = [t for t in tasks.values() if not is_ours(t)]
    tagged = [t for t in user_tasks if t.get("domain_tags")]
    hits = collections.Counter(tag for t in tagged for tag in (t.get("domain_tags") or []))
    out["domains"] = {
        "classified_tasks": len(tagged),
        "user_tasks": len(user_tasks),
        "platform_test_tasks": len(tasks) - len(user_tasks),
        "tags": [(taxonomy.DOMAIN_LABELS.get(k, k), v, round(v / len(tagged) * 100))
                 for k, v in hits.most_common()] if tagged else [],
        "lang": dict(collections.Counter(t.get("prompt_lang") for t in tagged if t.get("prompt_lang"))),
    }
    active = {t["user_id"] for t in user_tasks if t.get("user_id")}
    out["engagement"] = {
        "users_who_ran_something": len(active),
        "activation_rate_pct": round(len(active) / max(len(users), 1) * 100, 1),
    }

    # ---------- web traffic ----------
    # Only exists from the day sql/page_events.sql was applied — there is no
    # way to reconstruct visits or dwell time for anything before that.
    if sb.table_exists("page_events"):
        ev = sb.select("page_events", "select=session_id,path,user_id,started_at,last_seen_at,referrer")
        per_session = collections.defaultdict(lambda: {"secs": 0.0, "paths": set(), "user": None})
        for e in ev:
            f = "%Y-%m-%dT%H:%M:%S"
            try:
                secs = (datetime.datetime.strptime(e["last_seen_at"][:19], f)
                        - datetime.datetime.strptime(e["started_at"][:19], f)).total_seconds()
            except Exception:
                secs = 0.0
            row = per_session[e["session_id"]]
            row["secs"] += max(secs, 0.0)
            row["paths"].add(e["path"])
            row["user"] = row["user"] or e.get("user_id")
        dwell = sorted(r["secs"] for r in per_session.values())
        out["web"] = {
            "visits": len(per_session),
            "page_views": len(ev),
            "signed_in_visits": sum(1 for r in per_session.values() if r["user"]),
            "median_dwell_sec": round(dwell[len(dwell) // 2]) if dwell else 0,
            "over_1min_pct": round(sum(1 for d in dwell if d >= 60) / len(dwell) * 100) if dwell else 0,
            "by_path": dict(collections.Counter(e["path"] for e in ev)),
            "referrers": collections.Counter(e["referrer"] for e in ev if e.get("referrer")).most_common(6),
        }
    else:
        out["web"] = None

    # ---------- print ----------
    u = out["users"]; a = out["affiliations"]; v = out["volume"]; d = out["domains"]
    print("=" * 72)
    print(f"注册用户 {u['registered']}（已验证 {u['confirmed']}）"
          f" | 跑过任务的 {out['engagement']['users_who_ran_something']} 人"
          f"（{out['engagement']['activation_rate_pct']}%）")
    print(f"回访: 注册次日之后还登录过 {u['returned_after_day_one']} 人,"
          f" 30 天后仍回来 {u['returned_after_30_days']} 人")
    print("注册按月:", " ".join(f"{m}:{n}" for m, n in u["signups_by_month"].items()))
    print("=" * 72)
    print(f"单位: {a['usable']} 条有效填写 / {a['profiles']} profiles"
          f" → {a['distinct_institutions']} 家不同机构  {a['by_type']}")
    for name, n in a["top"][:12]:
        print(f"   {n:3d}×  {name}")
    print("=" * 72)
    print(f"任务量: 累计提交上界 {v['ever_submitted_upper_bound']}"
          f" | 有据可查 {v['evidence_rows']}"
          f"（实时表 {v['live_rows']} + 归档 {out['archive_rows']} + Storage {v['storage_folders']}）")
    print("按月:", " ".join(f"{m}:{n}" for m, n in v["by_month"].items()))
    print("=" * 72)
    print(f"领域分布（真实用户任务 {d['user_tasks']} 个, 其中 {d['classified_tasks']} 个可分类;"
          f" 平台自测 {d['platform_test_tasks']} 个已剔除）  语言 {d['lang']}")
    for label, n, pct in d["tags"]:
        print(f"   {n:3d} ({pct:3d}%)  {label}")

    w = out["web"]
    print("=" * 72)
    if w is None:
        print("访问统计: 未启用 — 在 Supabase SQL Editor 应用 sql/page_events.sql 后开始累积")
    elif w["visits"] == 0:
        print("访问统计: 已启用,尚无数据(埋点刚上线)")
    else:
        print(f"访问统计: {w['visits']} 次访问 / {w['page_views']} 次页面浏览"
              f" | 登录状态访问 {w['signed_in_visits']}")
        print(f"  停留时长中位数 {w['median_dwell_sec']}s,停留超过 1 分钟的访问占 {w['over_1min_pct']}%")
        print("  页面:", w["by_path"])
        if w["referrers"]:
            print("  来源:", ", ".join(f"{k}×{v}" for k, v in w["referrers"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\nJSON → {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
