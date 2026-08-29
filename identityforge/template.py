"""
Generates the upload template.

Per the house rule for a workbook someone else fills in: a legend naming the
cells to edit, and example rows showing realistic values in the expected
format. Three sheets: Instructions, Input (with examples to delete), Reference.
"""

from __future__ import annotations

import io

from .authorities import OCCUPATION_BUCKETS
from .bulk import COUNTRY_QIDS, INPUT_COLUMNS, OUTPUT_COLUMNS

EXAMPLE_ROWS = [
    ["1", "A. R. Rahman", "musician", 2010, "India",
     "Slumdog Millionaire soundtrack", "Sony Music",
     "punctuation variant of the name is fine"],
    ["2", "Michael Jackson", "musician", 2005, "United States",
     "Sony catalogue", "Sony Music",
     "same name as a footballer and a beer writer - role decides"],
    ["3", "Michael Jackson", "athlete", 2000, "United Kingdom", "", "",
     "same name, different person, different role hint"],
    ["4", "Christopher Nolan", "director", 2023, "United Kingdom",
     "Oppenheimer", "Universal", ""],
    ["5", "Priyanka Chopra", "actor", 2022, "India", "Citadel", "Prime Video",
     "married name Priyanka Chopra Jonas also matches"],
    ["6", "शाहरुख़ ख़ान", "actor", 2023, "India", "Jawan", "",
     "non-Latin script is supported - searched in its own language"],
]

NOTES = [
    ("name", "REQUIRED", "The person's name as you have it. Punctuation, "
     "missing diacritics and initials variants are handled ('A.R. Rahman', "
     "'AR Rahman', 'Beyonce'). Non-Latin scripts are searched in their own "
     "language."),
    ("role", "Strongly recommended", "The single most valuable field. Without "
     "it, a shared name returns 'disambiguate' instead of an answer. You "
     "usually already have it: title_category implies it."),
    ("active_year", "Optional", "Year the person was relevant to this request. "
     "Rules out candidates who were dead or unborn then."),
    ("country", "Optional", "Country name or a Wikidata Q-id. Helps separate "
     "same-name people in different markets."),
    ("context", "Optional", "Show, brand or campaign. Recorded for audit; not "
     "used for matching yet."),
    ("client", "Optional", "Your account or brand set. Recorded for audit."),
    ("row_id", "Optional", "Your own identifier, echoed back so you can join "
     "the results to your source sheet. Auto-numbered if blank."),
    ("notes", "Optional", "Free text, passed through to the output."),
]

DECISIONS = [
    ("resolved", "One person matched on role. Handles in the platform columns "
     "are accepted (confidence >= 0.75)."),
    ("disambiguate", "Several people share this name and the role hint did not "
     "separate them. The alternates column lists the candidates - pick one and "
     "re-run that row with a tighter role or country."),
    ("unverified_only", "No entity in the identity graph, so search proposals "
     "only. UNVERIFIED - confirm before use. Common for micro-influencers."),
    ("not_found", "No matching human found at all. Check the spelling against "
     "the person's Wikipedia article."),
    ("error", "Something failed on that row; see notes."),
]


def build_template_xlsx() -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    NAVY = "16243F"
    ACCENT = "33449C"
    wb = Workbook()

    head_font = Font(name="Arial", bold=True, color="FFFFFF", size=10)
    head_fill = PatternFill("solid", fgColor=NAVY)
    body = Font(name="Arial", size=10)
    bold = Font(name="Arial", bold=True, size=10)
    title = Font(name="Arial", bold=True, size=14, color=NAVY)
    # yellow marks cells the user should fill in
    input_fill = PatternFill("solid", fgColor="FFF9DB")
    example_fill = PatternFill("solid", fgColor="EDF1F7")
    example_font = Font(name="Arial", size=10, italic=True, color="5A6B87")
    thin = Side(style="thin", color="D9E0EA")
    box = Border(left=thin, right=thin, top=thin, bottom=thin)
    wrap = Alignment(vertical="top", wrap_text=True)

    # ---------------- Instructions ----------------
    ws = wb.active
    ws.title = "Instructions"
    ws["A1"] = "Talent Social Media Finder - bulk upload template"
    ws["A1"].font = title
    ws["A3"] = ("Fill in the Input sheet, then upload it at "
                "https://talent-social-finder.onrender.com")
    ws["A3"].font = body
    ws["A4"] = ("Delete the six grey example rows before uploading. Only "
                "'name' is required; everything else improves accuracy.")
    ws["A4"].font = body

    ws["A6"] = "Columns"
    ws["A6"].font = Font(name="Arial", bold=True, size=11, color=NAVY)
    ws.append([])
    r = 7
    for c, label in enumerate(["Column", "Required?", "What it does"], start=1):
        cell = ws.cell(row=r, column=c, value=label)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = wrap
    for name, req, desc in NOTES:
        r += 1
        for c, val in enumerate([name, req, desc], start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.font = bold if c == 1 else body
            cell.alignment = wrap
            cell.border = box

    r += 3
    ws.cell(row=r, column=1, value="What the decision column means").font = \
        Font(name="Arial", bold=True, size=11, color=NAVY)
    r += 1
    for c, label in enumerate(["decision", "meaning"], start=1):
        cell = ws.cell(row=r, column=c, value=label)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = wrap
    for dec, meaning in DECISIONS:
        r += 1
        for c, val in enumerate([dec, meaning], start=1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.font = bold if c == 1 else body
            cell.alignment = wrap
            cell.border = box

    r += 3
    ws.cell(row=r, column=1, value="Limits").font = \
        Font(name="Arial", bold=True, size=11, color=NAVY)
    for line in [
        "A live run costs roughly 5-10 seconds per name, because upstream rate "
        "limits are respected deliberately.",
        "One upload processes rows under a ~75 second budget and returns the "
        "rest as 'unprocessed'. Resubmit those; the cache makes pass two faster.",
        "For a sheet of hundreds of names, upload in batches of about 20-30, or "
        "run it locally where there is no HTTP timeout.",
        "A name match alone never produces a handle. Rows without corroboration "
        "come back as review or unverified, by design.",
    ]:
        r += 1
        cell = ws.cell(row=r, column=1, value="- " + line)
        cell.font = body
        cell.alignment = wrap

    ws.column_dimensions["A"].width = 22
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 86
    for row in ws.iter_rows(min_row=3, max_row=r):
        ws.row_dimensions[row[0].row].height = None

    # ---------------- Input ----------------
    inp = wb.create_sheet("Input")
    inp.append(INPUT_COLUMNS)
    for c in range(1, len(INPUT_COLUMNS) + 1):
        cell = inp.cell(row=1, column=c)
        cell.font = head_font
        cell.fill = head_fill
        cell.alignment = Alignment(vertical="center", wrap_text=True)

    for ex in EXAMPLE_ROWS:
        inp.append(ex)
        for c in range(1, len(INPUT_COLUMNS) + 1):
            cell = inp.cell(row=inp.max_row, column=c)
            cell.font = example_font
            cell.fill = example_fill
            cell.border = box

    # blank rows the operator fills in, marked yellow
    for _ in range(40):
        inp.append([""] * len(INPUT_COLUMNS))
        for c in range(1, len(INPUT_COLUMNS) + 1):
            cell = inp.cell(row=inp.max_row, column=c)
            cell.font = body
            cell.fill = input_fill
            cell.border = box

    roles = sorted(OCCUPATION_BUCKETS.keys())
    dv = DataValidation(type="list", formula1='"' + ",".join(roles) + '"',
                        allow_blank=True, showDropDown=False)
    dv.error = "Pick a role from the list, or leave blank."
    dv.prompt = "Optional but strongly recommended - it is what resolves collisions."
    inp.add_data_validation(dv)
    dv.add(f"C2:C{inp.max_row}")

    widths = {"row_id": 9, "name": 26, "role": 15, "active_year": 12,
              "country": 18, "context": 32, "client": 16, "notes": 44}
    for i, col in enumerate(INPUT_COLUMNS, start=1):
        inp.column_dimensions[get_column_letter(i)].width = widths.get(col, 16)
    inp.freeze_panes = "B2"

    # ---------------- Reference ----------------
    ref = wb.create_sheet("Reference")
    ref["A1"] = "Valid role values"
    ref["A1"].font = Font(name="Arial", bold=True, size=11, color=NAVY)
    ref["A2"] = "Use exactly these. Common synonyms are mapped automatically."
    ref["A2"].font = body
    r = 4
    ref.cell(row=r, column=1, value="role").font = head_font
    ref.cell(row=r, column=1).fill = head_fill
    ref.cell(row=r, column=2, value="matches occupations like").font = head_font
    ref.cell(row=r, column=2).fill = head_fill
    hints = {
        "actor": "actor, actress, film/TV/voice actor",
        "director": "film director, producer",
        "musician": "singer, composer, songwriter, rapper, band",
        "athlete": "footballer, cricketer, basketball player",
        "creator": "YouTuber, streamer, influencer, content creator",
        "model": "model, fashion model",
        "journalist": "journalist, news anchor",
        "politician": "politician",
        "writer": "author, novelist, screenwriter",
        "executive": "CEO, founder, businessperson",
        "comedian": "comedian, stand-up",
        "chef": "chef, restaurateur",
    }
    for role in roles:
        r += 1
        ref.cell(row=r, column=1, value=role).font = bold
        ref.cell(row=r, column=2, value=hints.get(role, "")).font = body
        ref.cell(row=r, column=1).border = box
        ref.cell(row=r, column=2).border = box

    r += 3
    ref.cell(row=r, column=1, value="Recognised country names").font = \
        Font(name="Arial", bold=True, size=11, color=NAVY)
    r += 1
    ref.cell(row=r, column=1,
             value="Any of these, or a raw Wikidata Q-id. "
                   "Unrecognised values pass through unchanged.").font = body
    r += 1
    names = sorted({k.title() for k in COUNTRY_QIDS})
    for i in range(0, len(names), 6):
        r += 1
        ref.cell(row=r, column=1, value=", ".join(names[i:i + 6])).font = body

    r += 3
    ref.cell(row=r, column=1, value="Output columns you will get back").font = \
        Font(name="Arial", bold=True, size=11, color=NAVY)
    r += 1
    ref.cell(row=r, column=1, value=", ".join(OUTPUT_COLUMNS)).font = body
    ref.cell(row=r, column=1).alignment = wrap

    ref.column_dimensions["A"].width = 30
    ref.column_dimensions["B"].width = 60

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def build_template_csv() -> bytes:
    import csv
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(INPUT_COLUMNS)
    for ex in EXAMPLE_ROWS:
        w.writerow(ex)
    return buf.getvalue().encode("utf-8-sig")
