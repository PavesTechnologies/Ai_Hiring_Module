# Duplicate-resume test files — AIRS upload decisions

Synthetic files for trying the duplicate-resume decisions by hand in the dev
app, against the **[P3] Junior Python Developer** campaign (Phase 3 fixtures).
All people and companies are invented; contact details use `example.invalid`.

- `F1_v2.docx` — F1 (Aarav Menon) with **the same name and email**, plus one
  extra project. Uploading it to a campaign where F1 already is should ask
  "Keep existing resume" or "Use new version".
- `dup_bulk_F1v2_F2.zip` (built, not committed) — `F1_v2.docx` plus a
  byte-identical copy of `tests/fixtures/phase3/F2.docx`. In a bulk upload to
  the same campaign, F2 should be skipped as **Duplicate** and F1_v2 should
  appear under **Needs decision**.

Rebuild with `python scripts/fixtures/build_duplicate_fixtures.py` (from the
repo root).

---

### F1_v2 — Aarav Menon

aarav.menon@example.invalid · +91 00000 00001 · Bengaluru

**Summary**
Computer Science graduate with a year of internship experience building Python backend services.

**Education**
B.Tech, Computer Science and Engineering — Lakeside Institute of Technology, 2026 (CGPA 8.4)

**Experience**
*Software Engineering Intern* — Cobalt Apps Pvt Ltd · Jan 2026 – Jun 2026
- Built REST APIs in FastAPI for an internal ticketing tool
- Wrote SQL queries and Alembic migrations on PostgreSQL
- Added pytest unit tests, raising coverage from 40% to 72%

*Backend Developer Intern* — Greenleaf Software · Jul 2025 – Dec 2025
- Maintained Django REST endpoints and fixed bugs from code review
- Used Git with feature branches and pull requests
- Containerised a service with Docker for local development

**Projects**
- *Library API*: Python, FastAPI, PostgreSQL, deployed on AWS EC2
- *Expense Tracker*: Python, Flask, SQLite, with pytest unit tests and a GitHub Actions workflow

**Skills**
Python, SQL, REST APIs, Git, FastAPI, Django, pytest, Docker, Linux, AWS
