import io
import logging
import zipfile
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.enums.constants import ActionType, EntityType
from app.exception_handler.exceptions import NotFoundError
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_note_repository import CandidateNoteRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.consent_repository import ConsentRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.email_notification_repository import EmailNotificationRepository
from app.repositories.interview_schedule_repository import InterviewScheduleRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.candidate_change_notifier import CandidateChangeNotifier
from app.repositories.candidate_composite_score_history_repository import (
    CandidateCompositeScoreHistoryRepository,
)

logger = logging.getLogger(__name__)

# Matches the literal every other resume-touching service already uses
# (ResumeService.RESUME_STORAGE_BUCKET, ResumeUploadService.RESUME_STORAGE_BUCKET,
# CampaignCandidateService._RESUME_STORAGE_BUCKET) — not worth a shared
# constant module just for this one string.
_RESUME_STORAGE_BUCKET = "airs_resumes"


class CandidateErasureService:
    """
    GDPR-style right-to-erasure — permanently removes a candidate and every
    row that references them (resumes, resume files in storage, scores,
    skills, embeddings, pipeline/campaign history, consent, notifications),
    regardless of whether the candidate originated from an individual or a
    bulk ZIP upload. Both origins land in identical tables (only
    Resume.bulk_upload_job_id differs), and bulk_upload_jobs/
    bulk_upload_job_files hold no live reference back to the candidate, so
    no bulk-specific handling is needed here.

    The one deliberate exception: the audit_log entry this method itself
    writes survives, since AuditLog.entity_id is a bare UUID with no FK to
    candidates.id — that's what makes the audit trail requirement and the
    "no trace" requirement compatible.
    """

    def __init__(
        self,
        candidate_repo: CandidateRepository,
        resume_repo: ResumeRepository,
        campaign_candidate_repo: CampaignCandidateRepository,
        consent_repo: ConsentRepository,
        email_notification_repo: EmailNotificationRepository,
        celery_task_log_repo: CeleryTaskLogRepository,
        dead_letter_queue_repo: DeadLetterQueueRepository,
        storage_service,
        audit_service: AuditService,
        composite_score_history_repo: CandidateCompositeScoreHistoryRepository,
        candidate_note_repo: CandidateNoteRepository | None = None,
        interview_schedule_repo: InterviewScheduleRepository | None = None,
        notifier: CandidateChangeNotifier | None = None,
        upload_decision_service=None,
        encryption_service=None,
        bulk_upload_job_repo=None,
        bulk_upload_job_file_repo=None,
    ):
        self.candidate_repo = candidate_repo
        self.resume_repo = resume_repo
        self.campaign_candidate_repo = campaign_candidate_repo
        self.consent_repo = consent_repo
        self.email_notification_repo = email_notification_repo
        self.celery_task_log_repo = celery_task_log_repo
        self.dead_letter_queue_repo = dead_letter_queue_repo
        self.storage_service = storage_service
        self.audit_service = audit_service
        self.composite_score_history_repo = composite_score_history_repo
        # Optional so existing constructor call sites keep working; when
        # absent their tables are simply not cleared, which is only safe
        # for a candidate that has neither notes nor interviews. The DI
        # provider always supplies both.
        self.candidate_note_repo = candidate_note_repo
        self.interview_schedule_repo = interview_schedule_repo
        self.notifier = notifier or CandidateChangeNotifier()
        # Duplicate resume decisions (UploadDecisionService) - optional like
        # the two above; the DI provider always supplies it.
        self.upload_decision_service = upload_decision_service
        # Bulk-upload and task-log traces (_erase_upload_and_log_traces) -
        # optional like the above; the DI provider always supplies all three.
        self.encryption_service = encryption_service
        self.bulk_upload_job_repo = bulk_upload_job_repo
        self.bulk_upload_job_file_repo = bulk_upload_job_file_repo

    def erase_candidate(
        self,
        candidate_id: UUID,
        actor_id: str,
        actor_role: str | None,
        reason: str | None = None,
    ) -> None:
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        try:
            resumes = self.resume_repo.get_all_versions_by_candidate(candidate_id)
            campaign_candidates = self.campaign_candidate_repo.get_by_candidate_id(candidate_id)
            had_bulk_origin = any(resume.bulk_upload_job_id is not None for resume in resumes)
            # Plain values captured before delete - the rows are gone after commit.
            erased_resume_ids = [resume.id for resume in resumes]
            removed = [(cc.campaign_id, cc.id) for cc in campaign_candidates]

            # Upload decisions (any status) go too; a PENDING one's held bulk
            # file is deleted from storage like the resume files below.
            held_files = []
            if self.upload_decision_service is not None:
                held_files = self.upload_decision_service.discard_for_erased_candidate(candidate_id)
            for path in held_files:
                self._delete_resume_file(path)

            # Their bulk-upload file rows, their entries in stored zips, and
            # task-log / dead-letter rows that name them (EMAIL_SEND included).
            traces = self._erase_upload_and_log_traces(candidate, resumes)

            self.resume_repo.delete_embeddings_by_candidate(candidate_id)
            self.resume_repo.delete_candidate_skills_by_candidate(candidate_id)

            # Must run before the campaign_candidates loop below —
            # email_notifications.campaign_candidate_id is a FK to
            # campaign_candidates.id, so deleting a campaign_candidate first
            # violates that constraint on any notification still pointing at it.
            self.email_notification_repo.delete_by_candidate(candidate_id)

            for campaign_candidate in campaign_candidates:
                # candidate_rejections is gone - the AI evaluation row
                # cascades automatically (cascade="all, delete-orphan" on
                # CampaignCandidate.ai_evaluation) when the campaign_candidate
                # itself is deleted below.
                self.campaign_candidate_repo.delete_stage_history(campaign_candidate.id)
                # Distinct table from stage_history above: the per-transition
                # log. Its FK blocked the whole erasure for any candidate
                # that had ever changed pipeline stage.
                self.campaign_candidate_repo.delete_stage_transition_log(campaign_candidate.id)
                if self.candidate_note_repo is not None:
                    self.candidate_note_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                if self.interview_schedule_repo is not None:
                    # Clears the interview rounds and their own four
                    # referencing tables (interviewers, history, feedback,
                    # interview-related email notifications).
                    self.interview_schedule_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.composite_score_history_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                # v1 / v2 scoring results and a shadow-report recruiter rank.
                self.campaign_candidate_repo.delete_scoring_results(campaign_candidate.id)
                self.campaign_candidate_repo.delete_shadow_ranking(campaign_candidate.id)
                # DLQ before celery_task_log — dead_letter_queue.original_task_id
                # is a NOT NULL FK to celery_task_log.task_id.
                self.dead_letter_queue_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.celery_task_log_repo.delete_by_campaign_candidate_id(campaign_candidate.id)
                self.campaign_candidate_repo.delete(campaign_candidate)

            for resume in resumes:
                self.resume_repo.delete_parse_attempts(resume.id)
                self.resume_repo.delete_skill_detection_data(resume.id)
                self.dead_letter_queue_repo.delete_by_resume_id(resume.id)
                self.celery_task_log_repo.delete_by_resume_id(resume.id)
                self._delete_resume_file(resume.file_path)
                self.resume_repo.delete(resume)

            self.consent_repo.delete_by_candidate(candidate_id)
            self.candidate_repo.delete(candidate)

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_DATA_ERASED,
                entity_type=EntityType.CANDIDATE,
                entity_id=candidate_id,
                details={
                    "resume_versions_deleted": len(resumes),
                    "campaign_candidates_deleted": len(campaign_candidates),
                    "upload_decision_files_deleted": len(held_files),
                    **traces,
                    "had_bulk_origin": had_bulk_origin,
                    "reason": reason,
                },
            )

            self.candidate_repo.commit()
        except Exception:
            self.candidate_repo.rollback()
            raise

        # Cached resume/candidate views hold decrypted names and emails -
        # erased data must not keep being served from Redis until TTL.
        self.notifier.invalidator.resumes(erased_resume_ids)
        for campaign_id, campaign_candidate_id in removed:
            self.notifier.removed(campaign_id, campaign_candidate_id)

    def request_erasure(
        self,
        candidate_id: UUID,
        actor_id: str,
        actor_role: str | None,
        reason: str | None = None,
    ) -> None:
        """
        Candidate Erasure - "requested" phase, distinct from erase_candidate
        (the full hard-delete flow above): an immediate, non-destructive
        privacy safeguard for the moment erasure is first requested,
        before the full cascade delete necessarily happens. Unlike
        erase_candidate:
          - resume_embeddings rows are RETAINED (never deleted) for
            referential integrity - each row's embedding vector is
            overwritten with a 384-dimension zero vector instead, via
            ResumeRepository.zero_out_embeddings_for_candidate.
          - is_talent_pool_eligible is set FALSE on every one of those
            rows (same repository call).
          - jd_embeddings is never touched - JDs are never candidate PII.
          - candidate.updated_at and every resume.updated_at are bumped
            (neither has an onupdate=func.now() at the DB level, so this
            must be done explicitly, same convention as every other
            manual updated_at bump in this codebase, e.g.
            deterministic_scoring_tasks.py's campaign_candidate.updated_at).

        Reuses ActionType.CANDIDATE_DATA_ERASED (already live in the DB
        enum - no new migration) for the audit log, with
        details.phase="requested" distinguishing it from erase_candidate's
        own (phase="completed") log entry.
        """
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        try:
            now = datetime.now(timezone.utc)

            self.candidate_repo.update_erasure_fields(candidate_id, erasure_requested_at=now)
            candidate.updated_at = now

            resumes = self.resume_repo.get_all_versions_by_candidate(candidate_id)
            for resume in resumes:
                resume.updated_at = now

            embeddings_zeroed = self.resume_repo.zero_out_embeddings_for_candidate(candidate_id)

            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.CANDIDATE_DATA_ERASED,
                entity_type=EntityType.CANDIDATE,
                entity_id=candidate_id,
                details={
                    "phase": "requested",
                    "resume_embeddings_zeroed": embeddings_zeroed,
                    "resume_versions_touched": len(resumes),
                    "reason": reason,
                },
            )

            self.candidate_repo.commit()
        except Exception:
            self.candidate_repo.rollback()
            raise

    # --- bulk-upload and task-log traces -----------------------------------------------

    def _personal_terms(self, candidate) -> list[str]:
        """The candidate's name, email and phone, decrypted - what task logs may quote."""
        if self.encryption_service is None:
            return []
        terms = []
        for field, minimum in (("full_name_encrypted", 5), ("email_encrypted", 3), ("phone_encrypted", 7)):
            blob = getattr(candidate, field, None)
            if not blob:
                continue
            try:
                value = self.encryption_service.decrypt(blob, candidate.encryption_key_id).strip()
            except Exception:
                logger.exception("Erasure: could not decrypt %s for candidate %s", field, candidate.id)
                continue
            if len(value) >= minimum:          # too short to search for safely
                terms.append(value)
        return terms

    def _erase_upload_and_log_traces(self, candidate, resumes) -> dict:
        """
        What the row-by-row erasure misses: the bulk-upload job-file rows of
        the candidate's files (they carry the uploaded file names), the
        candidate's entries inside the stored ZIPs (rebuilt without them, or
        deleted when nothing else remains), and task-log / dead-letter rows
        that quote their name, email, phone or - within their own bulk jobs
        only - their file names. Does not commit.
        """
        resume_paths = {resume.file_path for resume in resumes if resume.file_path}
        file_names = {resume.original_filename for resume in resumes if resume.original_filename}
        job_ids = sorted({resume.bulk_upload_job_id for resume in resumes if resume.bulk_upload_job_id},
                         key=str)

        job_files_deleted, zips_rebuilt, zips_deleted, zip_failures = 0, 0, 0, 0
        if self.bulk_upload_job_file_repo is not None:
            for job_id in job_ids:
                mine = [f for f in self.bulk_upload_job_file_repo.get_by_job_id(job_id)
                        if f.storage_path in resume_paths or f.original_filename in file_names]
                names_in_job = {f.original_filename for f in mine}
                file_names |= names_in_job
                for job_file in mine:
                    if job_file.storage_path not in resume_paths:      # e.g. a skipped duplicate's copy
                        self._delete_resume_file(job_file.storage_path)
                self.bulk_upload_job_file_repo.delete_by_ids([f.id for f in mine])
                job_files_deleted += len(mine)
                outcome = self._strip_zip(job_id, names_in_job)
                zips_rebuilt += outcome == "rebuilt"
                zips_deleted += outcome == "deleted"
                zip_failures += outcome == "failed"

        terms = self._personal_terms(candidate)
        task_ids = self.celery_task_log_repo.find_task_ids_mentioning(
            terms, scoped_terms=[name for name in file_names if len(name) >= 5], bulk_upload_job_ids=job_ids,
        )
        self.dead_letter_queue_repo.delete_for_erasure(task_ids, terms)
        self.celery_task_log_repo.delete_by_task_ids(task_ids)

        return {
            "bulk_upload_job_files_deleted": job_files_deleted,
            "bulk_zips_rebuilt": zips_rebuilt,
            "bulk_zips_deleted": zips_deleted,
            "bulk_zip_failures": zip_failures,
            "task_logs_naming_candidate_deleted": len(task_ids),
        }

    def _strip_zip(self, job_id: UUID, names: set[str]) -> str:
        """
        Removes the candidate's entries (matched by file name, as the
        extraction names them) from one job's stored ZIP: rebuilt under a new
        path when other files remain, deleted when none do. The ZIP is only
        read at extraction, so neither breaks the job. Best-effort on
        storage, like resume files - logged, never blocks the DB erasure.
        """
        if self.bulk_upload_job_repo is None or not names:
            return "none"
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None or not job.zip_storage_path:
            return "none"
        old_path = job.zip_storage_path
        try:
            archive = zipfile.ZipFile(io.BytesIO(self.storage_service.download_file(_RESUME_STORAGE_BUCKET, old_path)))
            kept = [entry for entry in archive.infolist()
                    if entry.filename.rsplit("/", 1)[-1] not in names]
            if len(kept) == len(archive.infolist()):
                return "none"
            if not any(not entry.is_dir() for entry in kept):
                self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, old_path)
                job.zip_storage_path = None
                return "deleted"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as rebuilt:
                for entry in kept:
                    rebuilt.writestr(entry, archive.read(entry))
            new_path = f"{old_path.rsplit('/', 1)[0]}/{uuid4()}.zip" if "/" in old_path else f"{uuid4()}.zip"
            self.storage_service.upload_file(bucket_name=_RESUME_STORAGE_BUCKET, file_path=new_path,
                                             file_content=buffer.getvalue(), content_type="application/zip")
            job.zip_storage_path = new_path
            self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, old_path)
            return "rebuilt"
        except Exception:
            logger.exception("Erasure: could not remove the candidate's files from bulk ZIP of job %s", job_id)
            return "failed"

    def _delete_resume_file(self, file_path: str) -> None:
        """
        Best-effort, same convention as every other storage-cleanup call site
        in this codebase (JDProcessingPipeline, bulk_upload_tasks) — a
        transient storage-provider error must not block the legally-required
        DB-side erasure, so it's logged, not raised.
        """
        try:
            self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, file_path)
        except Exception:
            logger.exception("Failed to delete resume file '%s' from storage during candidate erasure.", file_path)
