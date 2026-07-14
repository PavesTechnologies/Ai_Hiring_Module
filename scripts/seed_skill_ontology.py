from pathlib import Path

from app.db.session import SessionLocal
from app.services.skill_seed_service import SkillSeedService
from app.utils.excel.skill_excel_reader import SkillExcelReader

SEED_FILE_PATH = Path(__file__).resolve().parent.parent / "seed_data" / "skill_ontology_seed_production.xlsx"

db = SessionLocal()

try:
    skills = SkillExcelReader.read(SEED_FILE_PATH)
    summary = SkillSeedService(db).seed_skills(skills)

    print(f"Inserted: {summary['inserted']}")
    print(f"Skipped: {summary['skipped']}")
    print(f"Failed: {summary['failed']}")
except Exception:
    db.rollback()
    raise
finally:
    db.close()
