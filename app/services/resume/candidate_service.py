from datetime import datetime, timezone
from uuid import UUID

from app.core.encryption_service import EncryptionService
from app.exceptions.candidate_exceptions import CandidateErasureBlockedException
from app.models.candidates import Candidate
from app.repositories.candidate_repository import CandidateRepository
from app.services.compliance.consent_service import ConsentService

CANDIDATE_PII_PURPOSE = "CANDIDATE_PII"


class CandidateService:
    def __init__(
        self,
        candidate_repo: CandidateRepository,
        encryption_service: EncryptionService,
        consent_service: ConsentService,
    ):
        self.candidate_repo = candidate_repo
        self.encryption_service = encryption_service
        self.consent_service = consent_service

    def get_or_create(
        self,
        full_name: str,
        email: str,
        jurisdiction: str,
        consent_source: str,
        phone: str | None = None,
        org_id: UUID | None = None,
        source_campaign_id: UUID | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Candidate:
        """
        Returns the existing candidate for this email if one exists (after
        confirming it isn't erasure-blocked), otherwise atomically creates a
        new encrypted candidate record plus its paired consent record.
        """
        email_hash = self.encryption_service.generate_hash(email)

        existing = self.candidate_repo.get_by_email_hash(email_hash)
        if existing is not None:
            self._raise_if_erasure_blocked(existing)
            return existing

        candidate = self._build_encrypted_candidate(
            full_name=full_name,
            email=email,
            email_hash=email_hash,
            phone=phone,
            jurisdiction=jurisdiction,
            org_id=org_id,
            source_campaign_id=source_campaign_id,
        )

        try:
            candidate, was_created = self.candidate_repo.create(candidate)

            if not was_created:
                # Lost a concurrent-insert race — the winner's request is
                # responsible for that candidate's consent record, not us.
                self._raise_if_erasure_blocked(candidate)
                return candidate

            self.consent_service.record_consent(
                candidate_id=candidate.id,
                jurisdiction=jurisdiction,
                source=consent_source,
                ip_address=ip_address,
                user_agent=user_agent,
            )

            # Denormalized snapshot on the candidate row itself, per spec.
            candidate.consent_given = True
            candidate.consent_timestamp = datetime.now(timezone.utc)
            candidate.consent_source = consent_source

            self.candidate_repo.commit()
        except Exception:
            self.candidate_repo.rollback()
            raise

        return candidate

    def _build_encrypted_candidate(
        self,
        full_name: str,
        email: str,
        email_hash: str,
        phone: str | None,
        jurisdiction: str,
        org_id: UUID | None,
        source_campaign_id: UUID | None,
    ) -> Candidate:
        full_name_encrypted, encryption_key_id = self.encryption_service.encrypt(
            full_name, CANDIDATE_PII_PURPOSE
        )
        email_encrypted, _ = self.encryption_service.encrypt(email, CANDIDATE_PII_PURPOSE)

        phone_encrypted = None
        phone_hash = None
        if phone:
            phone_encrypted, _ = self.encryption_service.encrypt(phone, CANDIDATE_PII_PURPOSE)
            phone_hash = self.encryption_service.generate_hash(phone)

        return Candidate(
            org_id=org_id,
            full_name_encrypted=full_name_encrypted,
            email_encrypted=email_encrypted,
            email_hash=email_hash,
            phone_encrypted=phone_encrypted,
            phone_hash=phone_hash,
            encryption_key_id=encryption_key_id,
            jurisdiction=jurisdiction,
            source_campaign_id=source_campaign_id,
        )

    @staticmethod
    def _raise_if_erasure_blocked(candidate: Candidate) -> None:
        if candidate.erasure_requested_at is not None or candidate.is_pii_deleted:
            raise CandidateErasureBlockedException()
