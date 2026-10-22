"""
Builds tests/fixtures/phase3/*.docx (JDs and F1-F8, D1-D8) and expectations.json
from tests/fixtures/phase3/README.md. Re-run after editing the README; the
.docx files are rewritten (same text, new file metadata).

Usage (from the repo root): python scripts/fixtures/build_phase3_fixtures.py
"""
import json
import re
from pathlib import Path

from docx import Document
from docx.shared import Pt

OUT = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "phase3"
README = (OUT / "README.md").read_text(encoding="utf-8")
LINES = README.splitlines()

INLINE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*)")
BOLD_LINE = re.compile(r"^\*\*(.+)\*\*$")

# Short rule names per resume (the "tests" column is prose; the example entry uses names).
TESTS = {
    "F1": ["internship_counts_as_experience"],
    "F2": ["project_only_skills", "experience_near_miss"],
    "F3": ["education_field_family", "internship_counts_as_experience"],
    "F4": ["education_field_family", "experience_near_miss"],
    "F5": ["spelling_normalisation", "compound_skill_split", "skill_alias"],
    "F6": ["weak_fit"],
    "F7": ["core_near_miss"],
    "F8": ["keyword_stuffing", "listed_only_skills", "experience_near_miss"],
    "D1": ["strong_domain_match", "certifications"],
    "D2": ["domain_paraphrase", "functional_score"],
    "D3": ["certification_mapping", "implied_skill"],
    "D4": ["skill_sense"],
    "D5": ["experience_near_miss"],
    "D6": ["no_domain_experience"],
    "D7": ["other_platform", "core_near_miss"],
    "D8": ["end_user_not_implementer"],
}


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
    doc.core_properties.author = "AIRS Phase 3 synthetic test data"
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


def build_jd(lines: list[str], filename: str) -> None:
    body = [line for line in lines[1:]]                     # drop the "## n." section heading
    first = next(i for i, line in enumerate(body) if BOLD_LINE.match(line.strip()))
    title = BOLD_LINE.match(body[first].strip()).group(1)
    doc = new_document(title)
    doc.add_heading(title, level=1)
    render(doc, body[first + 1:])
    doc.save(OUT / filename)


def build_resume(rid: str) -> None:
    lines = section(rf"^### {rid} — ", (r"^### ", r"^## "))
    name = lines[0].split(" — ", 1)[1].strip()
    doc = new_document(name)
    doc.add_heading(name, level=1)
    render(doc, lines[1:])
    doc.save(OUT / f"{rid}.docx")


def arrows(text: str) -> str:
    return text.replace("→", "->").strip()


def expectations() -> list[dict]:
    rows = []
    for line in section(r"^### Expected outcomes", (r"^### expectations\.json",)):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] in ("ID", "---") or not re.fullmatch(r"[FD]\d", cells[0]):
            continue
        rid, _tests, outcome, core, reason, risk = cells
        rows.append({
            "id": rid, "jd": "JD-F" if rid.startswith("F") else "JD-D", "tests": TESTS[rid],
            "expected_outcome": outcome, "expected_core": core,
            "reason": arrows(reason), "current_system_risk": arrows(risk),
        })
    return rows


build_jd(section(r"^## 2\. ", (r"^## 3\. ",)), "jd_fresher.docx")
build_jd(section(r"^## 3\. ", (r"^## 4\. ",)), "jd_domain.docx")
for rid in [f"F{i}" for i in range(1, 9)] + [f"D{i}" for i in range(1, 9)]:
    build_resume(rid)
rows = expectations()
assert [r["id"] for r in rows] == [f"F{i}" for i in range(1, 9)] + [f"D{i}" for i in range(1, 9)], rows
(OUT / "expectations.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
print("built", len(list(OUT.glob("*.docx"))), "docx;", len(rows), "expectations")
