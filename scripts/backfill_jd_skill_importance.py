"""
One-off backfill: set jd_skills.importance (CORE/SUPPORTING) on required JD
skills that were created without one - HR-approved (formerly unknown)
skills linked before SkillCurationService copied the importance over. The
value is read from the JD's own extraction (app/services/jd/jd_skill_importance.py);
skills its extraction doesn't list stay NULL - nothing is guessed.

Idempotent. Dry run by default:
    python -m scripts.backfill_jd_skill_importance           # show what would change
    python -m scripts.backfill_jd_skill_importance --apply   # write it
"""
import argparse

from app.db.session import SessionLocal
import app.models  # noqa: F401 - registers every mapper
from app.models.jd.job_descriptions import JobDescription
from app.models.skills import JDSkill, SkillOntology
from app.services.jd.jd_skill_importance import importance_from_extraction


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write the changes (default: dry run)")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        rows = (
            db.query(JDSkill, SkillOntology, JobDescription)
            .join(SkillOntology, SkillOntology.id == JDSkill.canonical_skill_id)
            .join(JobDescription, JobDescription.id == JDSkill.jd_id)
            .filter(JDSkill.mandatory.is_(True), JDSkill.importance.is_(None))
            .all()
        )
        changed = 0
        for jd_skill, skill, jd in rows:
            importance = importance_from_extraction(jd.extracted_json, skill.canonical_name, *(skill.aliases or []))
            if importance is None:
                continue
            changed += 1
            print(f"{jd.title[:30]!r:32} {skill.canonical_name:<28} -> {importance.value}")
            if args.apply:
                jd_skill.importance = importance
        print(f"{changed} of {len(rows)} unclassified required JD skill(s) to update.")
        if not args.apply:
            print("Dry run - nothing written. Re-run with --apply to write it.")
            db.rollback()
            return
        db.commit()
        print(f"Updated {changed} JD skill(s).")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
