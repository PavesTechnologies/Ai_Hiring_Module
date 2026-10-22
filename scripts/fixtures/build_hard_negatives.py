"""
Builds tests/fixtures/hard_negatives/H1-H10.docx and expectations.json from
its README.md, plus the two upload zips (hn_domain_H1-H6.zip,
hn_fresher_H7-H10.zip - not committed). Re-run after editing the README;
the .docx files are rewritten (same text, new file metadata).

Usage (from the repo root): python scripts/fixtures/build_hard_negatives.py
"""
import json
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.shared import Pt

OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "hard_negatives"
LINES = (OUT / "README.md").read_text(encoding="utf-8").splitlines()
IDS = [f"H{i}" for i in range(1, 11)]

INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")
BOLD_LINE = re.compile(r"^\*\*(.+)\*\*$")


def section(start_pattern: str, end_patterns: tuple[str, ...]) -> list[str]:
    start = next(i for i, line in enumerate(LINES) if re.match(start_pattern, line))
    end = next((i for i in range(start + 1, len(LINES)) if any(re.match(p, LINES[i]) for p in end_patterns)), len(LINES))
    return LINES[start:end]


def add_inline(paragraph, text: str) -> None:
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("*") and part.endswith("*"):
            paragraph.add_run(part[1:-1]).italic = True
        else:
            paragraph.add_run(part)


def new_document(title: str) -> Document:
    doc = Document()
    doc.core_properties.title = title
    doc.core_properties.author = "AIRS hard-negative synthetic test data"
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    return doc


def render(doc: Document, body: list[str]) -> None:
    for line in body:
        text = line.strip()
        if not text or text == "---":
            continue
        heading = BOLD_LINE.match(text)
        if heading:
            doc.add_heading(heading.group(1), level=2)
        elif text.startswith("- "):
            add_inline(doc.add_paragraph(style="List Bullet"), text[2:])
        else:
            add_inline(doc.add_paragraph(), text)


def build_resume(rid: str) -> None:
    lines = section(rf"^### {rid} — ", (r"^### ", r"^## "))
    name = lines[0].split(" — ", 1)[1].strip()
    doc = new_document(name)
    doc.add_heading(name, level=1)
    render(doc, lines[1:])
    doc.save(OUT / f"{rid}.docx")


def plain(text: str) -> str:
    """Table cell -> JSON text: markdown emphasis dropped, arrows as ->."""
    return re.sub(r"\*\*([^*]+)\*\*|\*([^*]+)\*", lambda m: m.group(1) or m.group(2), text).replace("→", "->").strip()


def expectations() -> list[dict]:
    rows = []
    for line in section(r"^## 4\. Expected outcomes", (r"^### What the high-risk",)):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 7 or not re.fullmatch(r"H\d+", cells[0]):
            continue
        rid, jd, trap, outcome, core, reason, likely = cells
        rows.append({
            "id": rid, "jd": {"D": "JD-D", "F": "JD-F"}[jd], "trap": plain(trap),
            "expected_outcome": outcome, "expected_core": core,
            "reason": plain(reason), "likely_system_result": plain(likely),
        })
    return rows


for rid in IDS:
    build_resume(rid)
rows = expectations()
assert [r["id"] for r in rows] == IDS, rows
(OUT / "expectations.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
for name, ids in (("hn_domain_H1-H6.zip", IDS[:6]), ("hn_fresher_H7-H10.zip", IDS[6:])):
    with zipfile.ZipFile(OUT / name, "w", zipfile.ZIP_DEFLATED) as z:
        for rid in ids:
            z.write(OUT / f"{rid}.docx", f"{rid}.docx")
print("built", len(list(OUT.glob("H*.docx"))), "docx;", len(rows), "expectations; 2 zips")
