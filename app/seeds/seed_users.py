import uuid

from app.db.session import SessionLocal
from app.models.identity import Organization, User, UserRole

db = SessionLocal()

try:
    org = Organization(
        id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        name="Paves Technologies",
    )

    db.add(org)
    db.flush()
    
    user = User(
        id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
        org_id=org.id,
        email="admin@paves.com",
        password_hash="dummy-password",
        role=UserRole.HR_ADMIN,
        full_name="System Administrator",
        is_active=True,
        mfa_enabled=False,
    )

    db.add(user)

    db.commit()

    print("Seeded successfully")

except Exception:
    db.rollback()
    raise

finally:
    db.close()