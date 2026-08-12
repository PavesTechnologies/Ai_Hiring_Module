"""
One-time import of a real user identity from Aiven into RDS, NOT a generic
seed/fixture row - deliberately has no SEED_ prefix or is_seed_data-style
tag, since it represents the actual hiring manager used in earlier
testing/review (M12 Step 4b), not throwaway data.

Source: Aiven `users` row for id 5100022, pasted directly by a human with
live Aiven access (this environment has no stored Aiven credentials/
connection string - verified before asking). org_id/updated_at were not
part of that paste and are left NULL, matching the columns actually
supplied.

Idempotent: safe to re-run, checks by id first.
"""
from datetime import datetime, timezone

from app.db.session import SessionLocal
from app.models.identity import User, UserRole

db = SessionLocal()

try:
    existing = db.query(User).filter(User.id == "5100022").first()
    if existing:
        print(f"User 5100022 already exists: {existing.email}")
    else:
        user = User(
            id="5100022",
            org_id=None,
            email="venipriya.p@pavestechnologies.com",
            password_hash="EXTERNAL_AUTH",
            role=UserRole.HIRING_MANAGER,
            full_name="Venipriya P",
            is_active=True,
            last_login_at=None,
            mfa_enabled=False,
            password_reset_token=None,
            password_reset_expires_at=None,
            created_at=datetime(2026, 7, 15, 11, 11, 43, 45467, tzinfo=timezone.utc),
        )
        db.add(user)
        print("Added user 5100022 (venipriya.p@pavestechnologies.com, HIRING_MANAGER)")

    db.commit()
    print("\nHiring manager import complete")

except Exception as e:
    db.rollback()
    print(f"Error importing hiring manager: {e}")
    raise

finally:
    db.close()
