"""
Builds tests/fixtures/duplicates/F1_v2.docx from its README.md (same rendering
as the Phase 3 fixtures), and the upload zip dup_bulk_F1v2_F2.zip (not
committed): F1_v2.docx plus a byte-identical copy of tests/fixtures/phase3/F2.docx.

Usage (from the repo root): python scripts/fixtures/build_duplicate_fixtures.py
"""
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.shared import Pt

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures"
OUT = FIXTURES / "duplicates"
LINES = (OUT / "README.md").read_text(encoding="utf-8").splitlines()
ZIP_NAME = "dup_bulk_F1v2_F2.zip"

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
    doc.core_properties.author = "AIRS duplicate-upload synthetic test data"
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


lines = section(r"^### F1_v2 — ", (r"^### ", r"^## "))
name = lines[0].split(" — ", 1)[1].strip()
doc = new_document(name)
doc.add_heading(name, level=1)
render(doc, lines[1:])
doc.save(OUT / "F1_v2.docx")

with zipfile.ZipFile(OUT / ZIP_NAME, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(OUT / "F1_v2.docx", "F1_v2.docx")
    z.write(FIXTURES / "phase3" / "F2.docx", "F2.docx")          # byte-identical copy
print("built F1_v2.docx and", ZIP_NAME)
