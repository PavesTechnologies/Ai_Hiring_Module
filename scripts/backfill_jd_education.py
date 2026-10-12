"""
One-off backfill: fill gaps in existing JDs' extracted_json.education from
their education_criteria (the recruiter's upload-time form values) - the
same merge new JDs now get at persistence (app/services/jd/jd_education_merge.py).

AI-extracted values are never overwritten; only null/UNKNOWN keys are
filled, and education is never left null. JDs with no extracted_json (never
AI-processed) are left alone. Idempotent.

Dry run by default:
    python -m scripts.backfill_jd_education           # show what would change
    python -m scripts.backfill_jd_education --apply   # write it
"""
import argparse

from app.db.session import SessionLocal
import app.models  # noqa: F401 - registers every mapper
from app.models.jd.job_descriptions import JobDescription
from app.services.jd.jd_education_merge import merge_education_with_criteria


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        changed = 0
        jds = db.query(JobDescription).filter(JobDescription.extracted_json.isnot(None)).all()
        for jd in jds:
            before = (jd.extracted_json or {}).get("education")
            after = merge_education_with_criteria(before, jd.education_criteria)
            if after == before:
                continue
            changed += 1
            filled = after.get("gap_filled_fields") or ("shape only" if before is not None else "was null")
            print(f"{jd.id}  v{jd.version_number}  active={jd.is_active_version}  {jd.title[:40]!r:44} filled: {filled}")
            if args.apply:
                jd.extracted_json = {**jd.extracted_json, "education": after}

        print(f"{changed} of {len(jds)} JD(s) to update.")
        if not args.apply:
            print("Dry run - nothing written. Re-run with --apply to write it.")
            db.rollback()
            return
        db.commit()
        print(f"Updated {changed} JD(s).")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
