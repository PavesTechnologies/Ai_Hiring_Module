from datetime import datetime, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.candidate_notes import CandidateNote
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_note_repository import CandidateNoteRepository
from app.schemas.campaign.candidate_note_schema import CandidateNoteResponse
from app.services.audit_service import AuditService


class CandidateNoteService:
    """M11-E04-S01 — add, edit, delete and count recruiter notes."""

    def __init__(
        self,
        note_repo: CandidateNoteRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        audit_service: AuditService,
        user_name_resolver=None,
    ):
        self.note_repo = note_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.audit_service = audit_service
        # Optional so the service works without the campaign repo wired in;
        # names simply fall back to the raw user id.
        self.user_name_resolver = user_name_resolver

    def _names(self, user_ids: list[str]) -> dict[str, str]:
        if not self.user_name_resolver or not user_ids:
            return {}
        try:
            return self.user_name_resolver(list({u for u in user_ids if u}))
        except Exception:
            # A name lookup failure must not hide the notes themselves.
            return {}

    def _to_response(self, note: CandidateNote, names: dict[str, str]) -> CandidateNoteResponse:
        return CandidateNoteResponse(
            id=note.id,
            campaign_candidate_id=note.campaign_candidate_id,
            note_text=note.note_text,
            created_by=note.created_by,
            created_by_name=names.get(note.created_by) or note.created_by,
            created_at=note.created_at,
            updated_at=note.updated_at,
            is_edited=note.updated_at is not None,
        )

    def _require_candidate(self, campaign_candidate_id: UUID):
        cc = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if cc is None:
            raise CampaignException("Campaign candidate not found.", 404)
        return cc

    def list_notes(self, campaign_candidate_id: UUID) -> list[CandidateNoteResponse]:
        self._require_candidate(campaign_candidate_id)
        notes = self.note_repo.list_for_candidate(campaign_candidate_id)
        names = self._names([n.created_by for n in notes])
        return [self._to_response(n, names) for n in notes]

    def note_counts(self, campaign_candidate_ids: list[UUID]) -> dict[str, int]:
        """T03 — badge counts for a page of candidates."""
        return self.note_repo.counts_for_candidates(campaign_candidate_ids)

    def add_note(
        self, campaign_candidate_id: UUID, note_text: str, actor_id: str, actor_role: str | None,
    ) -> CandidateNoteResponse:
        text = (note_text or "").strip()
        if not text:
            raise CampaignException("Note cannot be empty.", 422)

        cc = self._require_candidate(campaign_candidate_id)
        try:
            note = self.note_repo.add(
                CandidateNote(
                    campaign_candidate_id=campaign_candidate_id,
                    note_text=text,
                    created_by=actor_id,
                )
            )
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_NOTE_ADDED.value,
                entity_type=EntityType.CAMPAIGN_CANDIDATE.value,
                entity_id=campaign_candidate_id,
                campaign_id=cc.campaign_id,
                details={"title": "Recruiter note added", "note_id": str(note.id)},
            )
            self.note_repo.commit()
        except Exception:
            self.note_repo.rollback()
            raise
        return self._to_response(note, self._names([note.created_by]))

    def _own_note(self, note_id: UUID, actor_id: str, is_hr_admin: bool) -> CandidateNote:
        note = self.note_repo.get_by_id(note_id)
        if note is None:
            raise CampaignException("Note not found.", 404)
        # Authorship is the boundary: a recruiter may only change their own
        # commentary. HR_ADMIN can remove anything, for moderation.
        if note.created_by != actor_id and not is_hr_admin:
            raise CampaignException("You can only modify your own notes.", 403)
        return note

    def edit_note(
        self, note_id: UUID, note_text: str, actor_id: str, actor_role: str | None,
        is_hr_admin: bool = False,
    ) -> CandidateNoteResponse:
        text = (note_text or "").strip()
        if not text:
            raise CampaignException("Note cannot be empty.", 422)

        note = self._own_note(note_id, actor_id, is_hr_admin)
        cc = self.campaign_candidate_repo.get_by_id(note.campaign_candidate_id)
        try:
            note.note_text = text
            note.updated_at = datetime.now(timezone.utc)
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_NOTE_UPDATED.value,
                entity_type=EntityType.CAMPAIGN_CANDIDATE.value,
                entity_id=note.campaign_candidate_id,
                campaign_id=cc.campaign_id if cc else None,
                details={"title": "Recruiter note edited", "note_id": str(note.id)},
            )
            self.note_repo.commit()
        except Exception:
            self.note_repo.rollback()
            raise
        return self._to_response(note, self._names([note.created_by]))

    def delete_note(
        self, note_id: UUID, actor_id: str, actor_role: str | None, is_hr_admin: bool = False,
    ) -> None:
        note = self._own_note(note_id, actor_id, is_hr_admin)
        cc = self.campaign_candidate_repo.get_by_id(note.campaign_candidate_id)
        try:
            # Soft delete — the note leaves the UI but survives for audit.
            note.deleted_at = datetime.now(timezone.utc)
            note.deleted_by = actor_id
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_NOTE_DELETED.value,
                entity_type=EntityType.CAMPAIGN_CANDIDATE.value,
                entity_id=note.campaign_candidate_id,
                campaign_id=cc.campaign_id if cc else None,
                details={"title": "Recruiter note deleted", "note_id": str(note.id)},
            )
            self.note_repo.commit()
        except Exception:
            self.note_repo.rollback()
            raise
