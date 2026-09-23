#!/usr/bin/env python3
"""export_deck_data.py — the source-data workbook behind the marketing deck.

Every number on the two slides, with the query it came from and the caveat that
goes with it, so the deck can be re-checked or rebuilt without re-deriving
anything. Regenerate any time:

    python3 export_deck_data.py [-o path.xlsx]

Totals and shares are Excel formulas, not baked-in values — change a row and
the sheet re-adds itself.
"""

import argparse
import collections
import datetime
import os
import re

import taxonomy
from stats import canon_org, classify_org, NON_ANSWER
from supabase_rest import Supa

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="0F2740")
HEAD_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=11)
TITLE_FONT = Font(name=FONT, bold=True, size=14, color="0F2740")
NOTE_FONT = Font(name=FONT, size=9, italic=True, color="5C7180")
BODY = Font(name=FONT, size=11)
THIN = Side(style="thin", color="D5DEE4")
BOX = Border(bottom=THIN)


def sheet(wb, title, headers, widths):
    ws = wb.create_sheet(title)
    ws.sheet_view.showGridLines = False
    ws.append(headers)
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for c in ws[1]:
        c.font, c.fill = HEAD_FONT, HEAD_FILL
        c.alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 20
    ws.freeze_panes = "A2"
    return ws


def note(ws, row, text):
    ws.cell(row=row, column=1, value=text).font = NOTE_FONT


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output",
                    default=os.path.expanduser("~/Codes/cfdqanda/foam-agent-deck-data.xlsx"))
    args = ap.parse_args()
    sb = Supa()
    today = datetime.date.today().isoformat()

    users = sb.auth_users()
    profiles = sb.select("user_profiles", "select=id,organization")
    archive = sb.select("simulation_archive", "select=*&order=id")
    live = sb.select("simulations", "select=id,result_data,prompt,user_id")
    emails = {u["id"]: (u.get("email") or "").lower() for u in users}
    owner = {e.strip().lower() for e in
             os.environ.get("PLATFORM_TEST_EMAILS", "").split(",") if e.strip()}

    wb = Workbook()
    wb.remove(wb.active)

    # ---------------- 1. headline figures ----------------
    ws = sheet(wb, "Slide 1 Figures", ["Figure", "Value", "Source", "Caveat"], [34, 14, 46, 60])
    orgs = [(p.get("organization") or "").strip() for p in profiles]
    usable = [canon_org(o) for o in orgs if o and not NON_ANSWER.match(o)]
    academic = sum(1 for o in usable if classify_org(o) == "academic")
    gaps = []
    for u in users:
        if u.get("last_sign_in_at"):
            f = "%Y-%m-%dT%H:%M:%S"
            gaps.append((datetime.datetime.strptime(u["last_sign_in_at"][:19], f)
                         - datetime.datetime.strptime(u["created_at"][:19], f)).days)
    rows = [
        ("Registered users", len(users), "auth.users (never purged)", "Complete — safe to quote"),
        ("Verified email addresses", sum(1 for u in users if u.get("email_confirmed_at")),
         "auth.users.email_confirmed_at", ""),
        ("Distinct institutions", len(set(usable)),
         "user_profiles.organization, alias-normalised",
         "Self-reported; SJTU / 上海交通大学 / Shanghai Jiao Tong merged into one"),
        ("Academic share", None, "classified from institution name",
         "Formula: academic / usable answers"),
        ("Simulation tasks ever submitted", max(a["id"] for a in archive),
         "max(simulations.id) — the id sequence", "INCLUDES platform self-tests; say '750+ incl. internal validation'"),
        ("Users who returned after day one", sum(1 for g in gaps if g >= 1),
         "auth.users: last_sign_in_at > created_at", ""),
        ("Users still returning after 30 days", sum(1 for g in gaps if g >= 30),
         "auth.users: last_sign_in_at - created_at >= 30d", ""),
        ("End-to-end success rate", 0.679, "measured over 2026-07-14..08-14 (56 terminal tasks)",
         "DO NOT recompute from the live table: failures are purged at 30 days, successes at 90, so it reads ~80%"),
    ]
    for r in rows:
        ws.append(list(r))
    ws["B5"] = f"={academic}/{len(usable)}"
    ws["B5"].number_format = "0%"
    ws["B12"].number_format = "0.0%"
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=4):
        for c in row:
            c.font = BODY
            c.alignment = Alignment(vertical="top", wrap_text=(c.column == 4))
        row[0].border = BOX
    ws["B9"].number_format = "#,##0"
    note(ws, ws.max_row + 2, f"Generated {today} by export_deck_data.py from the live database.")

    # ---------------- 2. growth ----------------
    ws = sheet(wb, "User Growth", ["Month", "New users", "Cumulative"], [14, 14, 14])
    by_month = collections.Counter(u["created_at"][:7] for u in users)
    for i, m in enumerate(sorted(by_month), start=2):
        ws.cell(row=i, column=1, value=m).font = BODY
        ws.cell(row=i, column=2, value=by_month[m]).font = BODY
        c = ws.cell(row=i, column=3, value=f"=SUM($B$2:B{i})")
        c.font = BODY
    last = ws.max_row
    ws.cell(row=last + 1, column=1, value="Total").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 1, column=2, value=f"=SUM(B2:B{last})").font = Font(name=FONT, bold=True)
    note(ws, last + 3, "Cumulative column is the curve on slide 1. Registrations are never deleted, so this series is complete.")

    # ---------------- 3. institutions ----------------
    ws = sheet(wb, "Institutions", ["Institution (normalised)", "Users", "Type"], [46, 10, 16])
    counts = collections.Counter(usable)
    for i, (name, n) in enumerate(counts.most_common(), start=2):
        ws.cell(row=i, column=1, value=name).font = BODY
        ws.cell(row=i, column=2, value=n).font = BODY
        ws.cell(row=i, column=3, value=classify_org(name)).font = BODY
    last = ws.max_row
    ws.cell(row=last + 2, column=1, value="Distinct institutions").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 2, column=2, value=f"=COUNTA(A2:A{last})").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 3, column=1, value="Users with a usable answer").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 3, column=2, value=f"=SUM(B2:B{last})").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 4, column=1, value="Academic").font = BODY
    ws.cell(row=last + 4, column=2, value=f'=SUMIF(C2:C{last},"academic",B2:B{last})').font = BODY
    ws.cell(row=last + 5, column=1, value="Company").font = BODY
    ws.cell(row=last + 5, column=2, value=f'=SUMIF(C2:C{last},"company",B2:B{last})').font = BODY
    note(ws, last + 7, "Self-reported free text. 'unclassified' = abbreviations or entries the name rules could not place, not non-academic.")
    note(ws, last + 8, "Naming a specific institution in public material normally needs their permission — the aggregate count does not.")

    # ---------------- 4. slide 2 ----------------
    ws = sheet(wb, "Slide 2 Figures", ["CFD area", "User tasks", "Share of classified tasks"], [32, 12, 22])
    def is_ours(t):
        return t.get("is_platform_test") or emails.get(t.get("user_id"), "") in owner
    user_tasks = [a for a in archive if not is_ours(a)]
    tagged = [t for t in user_tasks if t.get("domain_tags")]
    hits = collections.Counter(tag for t in tagged for tag in t["domain_tags"])
    for i, (tag, n) in enumerate(hits.most_common(), start=2):
        ws.cell(row=i, column=1, value=taxonomy.DOMAIN_LABELS.get(tag, tag)).font = BODY
        ws.cell(row=i, column=2, value=n).font = BODY
        c = ws.cell(row=i, column=3, value=f"=B{i}/$B${ws.max_row + 2 + len(hits) - len(hits)}")
        c.font = BODY
    last = ws.max_row
    ws.cell(row=last + 2, column=1, value="Classified user tasks (denominator)").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 2, column=2, value=len(tagged)).font = Font(name=FONT, bold=True)
    for i in range(2, last + 1):
        ws.cell(row=i, column=3).value = f"=B{i}/$B${last + 2}"
        ws.cell(row=i, column=3).number_format = "0%"
    ws.cell(row=last + 3, column=1, value="User tasks in the archive").font = BODY
    ws.cell(row=last + 3, column=2, value=len(user_tasks)).font = BODY
    ws.cell(row=last + 4, column=1, value="Platform test tasks excluded").font = BODY
    ws.cell(row=last + 4, column=2, value=len(archive) - len(user_tasks)).font = BODY
    note(ws, last + 6, "A task can touch several areas, so shares add to more than 100%.")
    note(ws, last + 7, "Areas come from keyword rules in taxonomy.py, applied to the request text.")

    # ---------------- 5. solvers ----------------
    ws = sheet(wb, "Solvers Run", ["OpenFOAM application", "Tasks", "Role"], [26, 10, 30])
    UTIL = {"blockMesh", "snappyHexMesh", "checkMesh", "setFields", "topoSet", "decomposePar",
            "reconstructPar", "surfaceFeatures", "postProcess", "foamToVTK", "renumberMesh"}
    solvers, utils = collections.Counter(), collections.Counter()
    for r in live:
        files = ((r.get("result_data") or {}).get("file_tree") or {}).get("files") or []
        for f in {f["name"] for f in files}:
            m = re.match(r"^log\.(\w+)$", f)
            if m:
                (utils if m.group(1) in UTIL else solvers)[m.group(1)] += 1
    for i, (name, n) in enumerate(solvers.most_common(), start=2):
        ws.cell(row=i, column=1, value=name).font = BODY
        ws.cell(row=i, column=2, value=n).font = BODY
        ws.cell(row=i, column=3, value="solver").font = BODY
    off = ws.max_row + 1
    for i, (name, n) in enumerate(utils.most_common(), start=off):
        ws.cell(row=i, column=1, value=name).font = BODY
        ws.cell(row=i, column=2, value=n).font = BODY
        ws.cell(row=i, column=3, value="mesh / post-processing").font = BODY
    last = ws.max_row
    ws.cell(row=last + 2, column=1, value="Distinct solvers").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 2, column=2, value=f'=COUNTIF(C2:C{last},"solver")').font = Font(name=FONT, bold=True)
    note(ws, last + 4, "Counted from log.<application> files in the retained tasks only — the real figure is at least this.")

    # ---------------- 6. task volume ----------------
    ws = sheet(wb, "Task Volume", ["Month", "Tasks with evidence", "of which platform tests"], [14, 22, 24])
    per_month = collections.Counter(a["created_at"][:7] for a in archive if a.get("created_at"))
    test_month = collections.Counter(a["created_at"][:7] for a in archive
                                     if a.get("created_at") and a.get("is_platform_test"))
    for i, m in enumerate(sorted(per_month), start=2):
        ws.cell(row=i, column=1, value=m).font = BODY
        ws.cell(row=i, column=2, value=per_month[m]).font = BODY
        ws.cell(row=i, column=3, value=test_month.get(m, 0)).font = BODY
    last = ws.max_row
    ws.cell(row=last + 1, column=1, value="Total").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 1, column=2, value=f"=SUM(B2:B{last})").font = Font(name=FONT, bold=True)
    ws.cell(row=last + 1, column=3, value=f"=SUM(C2:C{last})").font = Font(name=FONT, bold=True)
    note(ws, last + 3, "Evidence only: the TTL purge removed ~84% of all tasks before the archive existed (766 created, 129 recoverable).")
    note(ws, last + 4, "Use this for shape, not for absolute volume; for totals quote the id sequence on 'Slide 1 Figures'.")

    # ---------------- 7. caveats ----------------
    ws = sheet(wb, "Read Me", ["Point", "Detail"], [34, 96])
    for a, b in [
        ("Generated", f"{today}, by cfdqanda-server/export_deck_data.py against the live database"),
        ("Deck", "foam-agent-traction-2026-09.pptx — every figure on those two slides comes from this workbook"),
        ("Success rate", "Quote 67.9% from the 2026-07-14..08-14 review. The live table over-states it: failed tasks are purged after 30 days, completed after 90"),
        ("Task totals", "766 is the id sequence, so it counts platform self-tests too. Roughly half of attributable historical tasks came from platform accounts"),
        ("Institutions", "Self-reported and alias-normalised. Aggregate counts are safe to publish; naming an institution usually needs its permission"),
        ("Domains", "Keyword classification of request text (taxonomy.py); multi-label, so shares exceed 100%"),
        ("Not available", "Visits, unique visitors and dwell time do not exist before 2026-09-22 — the site had no analytics until then"),
        ("Regenerate", "cd cfdqanda-server && python3 export_deck_data.py"),
    ]:
        ws.append([a, b])
    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, max_col=2):
        for c in row:
            c.font = BODY
            c.alignment = Alignment(vertical="top", wrap_text=(c.column == 2))

    wb.save(args.output)
    print("written:", args.output)


if __name__ == "__main__":
    main()
