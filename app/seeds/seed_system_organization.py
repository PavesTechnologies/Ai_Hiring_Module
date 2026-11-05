"""
The system organisation every campaign and JD belongs to (app.models.identity.SYSTEM_ORG_ID).

A base seed: POST /campaigns and the JD upload write this id, so a fresh database needs its row
before the first campaign. Idempotent - an existing row is left as it is.

    python -m app.seeds.seed_system_organization
"""
from app.models.identity import SYSTEM_ORG_ID, Organization

SYSTEM_ORG_NAME = "Paves Technologies"


def seed(db) -> bool:
    """Creates the row if it's missing; True when it was created."""
    if db.get(Organization, SYSTEM_ORG_ID) is not None:
        return False
    db.add(Organization(id=SYSTEM_ORG_ID, name=SYSTEM_ORG_NAME))
    db.commit()
    return True


if __name__ == "__main__":
    from app.db.session import SessionLocal

    session = SessionLocal()
    try:
        created = seed(session)
        print(f"System organisation {SYSTEM_ORG_ID}: {'created' if created else 'already present - left as it is'}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
