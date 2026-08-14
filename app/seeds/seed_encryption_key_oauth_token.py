import uuid

from app.db.session import SessionLocal
from app.models.config import EncryptionKey, KeyStatus

OAUTH_TOKEN_PURPOSE = "OAUTH_TOKEN"
DEFAULT_KEY_ALIAS = "oauth-token-v1"

db = SessionLocal()

try:
    existing_active = (
        db.query(EncryptionKey)
        .filter(
            EncryptionKey.purpose == OAUTH_TOKEN_PURPOSE,
            EncryptionKey.key_status == KeyStatus.ACTIVE,
        )
        .first()
    )

    if existing_active:
        print(
            f"An ACTIVE encryption key for purpose '{OAUTH_TOKEN_PURPOSE}' "
            f"already exists (alias='{existing_active.key_alias}') — skipping."
        )
    else:
        key = EncryptionKey(
            id=uuid.uuid4(),
            key_alias=DEFAULT_KEY_ALIAS,
            key_status=KeyStatus.ACTIVE,
            purpose=OAUTH_TOKEN_PURPOSE,
        )
        db.add(key)
        db.commit()
        print(f"Added ACTIVE encryption key: alias='{DEFAULT_KEY_ALIAS}', purpose='{OAUTH_TOKEN_PURPOSE}'")
        print(
            f"Reminder: the actual key material must be set in .env as "
            f"ENCRYPTION_KEY_{DEFAULT_KEY_ALIAS.upper().replace('-', '_')} "
            f"(a Fernet key) — this script only creates the database record."
        )

except Exception as e:
    db.rollback()
    print(f"Error seeding encryption key: {e}")
    raise

finally:
    db.close()
