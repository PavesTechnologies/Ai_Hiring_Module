"""
Re-compute candidates.email_hash / phone_hash with the current keyed lookup hash (deferred finding P-M3).

Dev only: production starts on a fresh database and only ever writes keyed hashes. Also needed after a
change of the lookup key (rotating the candidate-PII key it is derived from, or setting LOOKUP_HASH_KEY).

    python -m scripts.rehash_candidate_lookups            # dry run: counts only
    python -m scripts.rehash_candidate_lookups --apply    # writes, one transaction

Decrypts each candidate's email and phone with their own key; prints counts only, never values.
"""
import argparse
import sys

from app.core.encryption_service import EncryptionService
from app.db.session import SessionLocal
from app.models.candidates import Candidate
from app.repositories.encryption_key_repository import EncryptionKeyRepository


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--apply", action="store_true", help="write the new hashes (default: dry run)")
    args = parser.parse_args(argv)
    db = SessionLocal()
    try:
        service = EncryptionService(EncryptionKeyRepository(db))
        total = changed = failed = 0
        for candidate in db.query(Candidate).all():
            total += 1
            try:
                email = service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)
                phone = (service.decrypt(candidate.phone_encrypted, candidate.encryption_key_id)
                         if candidate.phone_encrypted else None)
            except Exception as exc:
                failed += 1
                print(f"candidate {candidate.id}: could not decrypt ({type(exc).__name__}) - left as is")
                continue
            email_hash = service.generate_hash(email)
            phone_hash = service.generate_hash(phone) if phone else None
            if (candidate.email_hash, candidate.phone_hash) != (email_hash, phone_hash):
                changed += 1
                candidate.email_hash, candidate.phone_hash = email_hash, phone_hash
        if args.apply:
            db.commit()
        else:
            db.rollback()
        print(f"candidates {total} | re-hashed {changed} | not decryptable {failed} | "
              f"{'written' if args.apply else 'dry run - nothing written'}")
        return 1 if failed else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
