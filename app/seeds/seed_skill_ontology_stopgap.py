"""
STOPGAP, not a fix: skill_ontology was found empty (0 rows, was 691) after
the 2026-08-07 RDS reset, and its real source file
(seed_data/skill_ontology_seed_production.xlsx, read by
scripts/seed_skill_ontology.py) is missing from this checkout entirely -
tracked in old commits 25b2f20/ec204e6 but absent from current HEAD's tree.

This inserts only the ~14 canonical skill names app/seeds/
seed_m12_test_campaign.py actually needs, tagged source="SEED_STOPGAP" so
they're trivially findable and deletable once the real 691-row dataset is
restored - this is NOT a substitute for that dataset, and nothing here
should be treated as curated ontology data (no aliases, no embeddings, no
category taxonomy beyond a rough guess).

Idempotent: checks for existing canonical_name before inserting.
"""
import uuid

from app.db.session import SessionLocal
from app.models.skills import SkillOntology

STOPGAP_SOURCE = "SEED_STOPGAP"

SKILLS = [
    ("Python", "Backend"),
    ("FastAPI", "Backend"),
    ("PostgreSQL", "Database"),
    ("SQLAlchemy", "Backend"),
    ("REST API", None),
    ("git", "Backend"),
    ("Docker", "DevOps"),
    ("Microservices", "Backend"),
    ("pytest", "Testing"),
    ("Django", "Backend"),
    ("Kubernetes", "DevOps"),
    ("AWS", "Cloud"),
    ("Redis", "Database"),
    ("Celery", "Backend"),
]

db = SessionLocal()

try:
    for name, category in SKILLS:
        existing = db.query(SkillOntology).filter(SkillOntology.canonical_name == name).first()
        if existing:
            print(f"skill_ontology already has '{name}' — skipping")
            continue
        db.add(SkillOntology(
            id=uuid.uuid4(),
            canonical_name=name,
            category=category,
            confidence="unverified",
            source=STOPGAP_SOURCE,
            is_active=True,
            occurrence_count=0,
        ))
        print(f"Added stopgap skill_ontology row: {name}")

    db.commit()
    print("\nSkill ontology stopgap seed complete — this is NOT the real 691-row dataset.")

except Exception as e:
    db.rollback()
    print(f"Error seeding skill_ontology stopgap: {e}")
    raise

finally:
    db.close()
