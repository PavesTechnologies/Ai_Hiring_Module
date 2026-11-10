import hashlib
import io
import logging
import re
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
# A staged bulk file's key: campaign_{id}/bulk-zip/{job_id}/{index:04d}_{name}
_STAGED_JOB = re.compile(r"bulk-zip/([0-9a-fA-F-]{36})/")


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
        document_processing_repo=None,
        skill_detection_repo=None,
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
        # Processing-stage rows and parse-cache entries (no FK to the resume) -
        # optional like the above; the DI provider always supplies both.
        self.document_processing_repo = document_processing_repo
        self.skill_detection_repo = skill_detection_repo

    def erase_candidate(
        self,
        candidate_id: UUID,
        actor_id: str,
        actor_role: str | None,
        reason: str | None = None,
    ) -> dict:
        """
        Review item 14: the database erasure commits first; the stored files are deleted after it, so a
        failure never leaves a candidate restored without their files. A file whose delete fails is
        retried by compliance.delete_erased_files (dead-lettered if it keeps failing) and reported in a
        follow-up audit entry and in the result: {"files_deleted", "files_pending_deletion"}.
        """
        candidate = self.candidate_repo.get_by_id(candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate {candidate_id} not found.")

        self._files_after_commit: list[str] = []     # deleted once the erasure is committed
        self._uploaded_this_run: list[str] = []      # rebuilt ZIPs - removed again if the erasure rolls back
        try:
            resumes = self.resume_repo.get_all_versions_by_candidate(candidate_id)
            campaign_candidates = self.campaign_candidate_repo.get_by_candidate_id(candidate_id)
            had_bulk_origin = any(resume.bulk_upload_job_id is not None for resume in resumes)
            # Plain values captured before delete - the rows are gone after commit.
            erased_resume_ids = [resume.id for resume in resumes]
            removed = [(cc.campaign_id, cc.id) for cc in campaign_candidates]
            # Review item 16: before their embeddings are deleted - an embedding task running now
            # re-checks its resume under this lock before writing, so it can't write a vector back.
            self.resume_repo.lock_for_erasure(erased_resume_ids)

            # Upload decisions (any status) go too; a PENDING one's held bulk
            # file is deleted from storage like the resume files below.
            held_files = []
            if self.upload_decision_service is not None:
                held_files = self.upload_decision_service.discard_for_erased_candidate(candidate_id)
            for path in held_files:
                self._delete_resume_file(path)

            # Review item 13: the task ids of their own parses - their resumes' task logs here, their
            # bulk job files' below - read before those rows are deleted. A failed parse's stage rows
            # are found only by these (they were never linked to a resume).
            parse_task_ids = set(self.celery_task_log_repo.get_task_ids_by_resume_ids(erased_resume_ids))

            # Their bulk-upload file rows, their entries in stored zips, and
            # task-log / dead-letter rows that name them (EMAIL_SEND included).
            traces = self._erase_upload_and_log_traces(candidate, resumes, parse_task_ids, held_files)
            # Before the resumes' derived rows go: those rows are where the hash
            # of each resume's text is recorded.
            traces.update(self._erase_processing_and_cache_traces(erased_resume_ids, parse_task_ids))

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
                    "reason_provided": bool(reason),
                    "files_to_delete": len(self._files_after_commit),
                },
            )

            self.candidate_repo.commit()
        except Exception:
            self.candidate_repo.rollback()
            self._remove_uploaded_this_run()
            raise

        # Cached resume/candidate views hold decrypted names and emails -
        # erased data must not keep being served from Redis until TTL.
        self.notifier.invalidator.resumes(erased_resume_ids)
        for campaign_id, campaign_candidate_id in removed:
            self.notifier.removed(campaign_id, campaign_candidate_id)

        return self._delete_files_after_commit(candidate_id, actor_id, actor_role)

    def _delete_files_after_commit(self, candidate_id: UUID, actor_id: str, actor_role: str | None) -> dict:
        """Deletes the queued files; failures go to the retry task and a follow-up audit entry (counts only)."""
        paths = list(dict.fromkeys(p for p in self._files_after_commit if p))
        failed = []
        for path in paths:
            try:
                self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, path)
            except Exception as exc:
                logger.warning("Erasure of candidate %s: a file delete failed (%s); retrying in the background.",
                               candidate_id, type(exc).__name__)
                failed.append(path)
        if failed:
            from app.tasks.compliance_tasks import queue_erased_file_deletes
            db = getattr(self.celery_task_log_repo, "db", None)
            retry_task_id = queue_erased_file_deletes(db, _RESUME_STORAGE_BUCKET, failed) if db is not None else None
            try:
                self.audit_service.log(
                    actor_id=actor_id, actor_role=actor_role, action_type=ActionType.CANDIDATE_DATA_ERASED,
                    entity_type=EntityType.CANDIDATE, entity_id=candidate_id,
                    details={"phase": "file_cleanup", "files_deleted": len(paths) - len(failed),
                             "files_not_deleted": len(failed), "retry_task_id": retry_task_id},
                )
                self.candidate_repo.commit()
            except Exception:
                logger.exception("Erasure of candidate %s: could not audit the pending file deletes.", candidate_id)
                self.candidate_repo.rollback()
        return {"files_deleted": len(paths) - len(failed), "files_pending_deletion": len(failed)}

    def _remove_uploaded_this_run(self) -> None:
        """The erasure rolled back: a ZIP rebuilt during it is not referenced by anything - remove it."""
        for path in self._uploaded_this_run:
            try:
                self.storage_service.delete_file(_RESUME_STORAGE_BUCKET, path)
            except Exception:
                logger.exception("Erasure rolled back: could not remove a rebuilt ZIP.")

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
                    "reason_provided": bool(reason),
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

    def _erase_upload_and_log_traces(self, candidate, resumes, parse_task_ids: set | None = None,
                                     held_paths=()) -> dict:
        """
        What the row-by-row erasure misses: the bulk-upload job-file rows of
        the candidate's files (they carry the uploaded file names), the
        candidate's entries inside the stored ZIPs (rebuilt without them, or
        deleted when nothing else remains), and task-log / dead-letter rows
        that quote their name, email or phone, or that belong to their own
        bulk files. Does not commit.

        Review item 15: a job file is theirs only by storage path - one of their resume files or held
        upload-decision files - or, for a file skipped as an identical duplicate, by its content hash
        matching one of their resumes. Never by file name: two "CV.pdf" files in one ZIP belong to
        different people.
        """
        own_paths = {resume.file_path for resume in resumes if resume.file_path} | {p for p in held_paths if p}
        resume_paths = {resume.file_path for resume in resumes if resume.file_path}
        own_hashes = {h for h in (getattr(resume, "file_hash", None) for resume in resumes) if h}
        job_ids = {resume.bulk_upload_job_id for resume in resumes if resume.bulk_upload_job_id}
        # A held file (pending upload decision) can sit in a job none of their resumes came from.
        job_ids |= {UUID(m.group(1)) for m in (_STAGED_JOB.search(p or "") for p in held_paths) if m}
        job_ids = sorted(job_ids, key=str)

        job_files_deleted, zips_rebuilt, zips_deleted, zip_failures = 0, 0, 0, 0
        own_file_task_ids: set[str] = set()
        if self.bulk_upload_job_file_repo is not None:
            for job_id in job_ids:
                job_files = self.bulk_upload_job_file_repo.get_by_job_id(job_id)
                mine = [f for f in job_files
                        if f.storage_path in own_paths or self._is_their_skipped_copy(f, own_hashes)]
                own_file_task_ids.update(t for t in (getattr(f, "task_id", None) for f in mine) if t)
                for job_file in mine:
                    if job_file.storage_path not in resume_paths:      # a skipped duplicate's copy, a held file
                        self._delete_resume_file(job_file.storage_path)
                self.bulk_upload_job_file_repo.delete_by_ids([f.id for f in mine])
                job_files_deleted += len(mine)
                others = [f for f in job_files if f not in mine]
                outcome = self._strip_zip(job_id, mine, others)
                zips_rebuilt += outcome == "rebuilt"
                zips_deleted += outcome == "deleted"
                zip_failures += outcome == "failed"
        if parse_task_ids is not None:
            parse_task_ids.update(own_file_task_ids)

        terms = self._personal_terms(candidate)
        task_ids = self.celery_task_log_repo.find_task_ids_mentioning(terms)
        task_ids = list(dict.fromkeys([*task_ids, *sorted(own_file_task_ids)]))
        self.dead_letter_queue_repo.delete_for_erasure(task_ids, terms)
        self.celery_task_log_repo.delete_by_task_ids(task_ids)

        return {
            "bulk_upload_job_files_deleted": job_files_deleted,
            "bulk_zips_rebuilt": zips_rebuilt,
            "bulk_zips_deleted": zips_deleted,
            "bulk_zip_failures": zip_failures,
            "task_logs_naming_candidate_deleted": len(task_ids),
        }

    def _is_their_skipped_copy(self, job_file, own_hashes: set[str]) -> bool:
        """A file skipped as identical to a resume already in the campaign: theirs if the bytes match."""
        from app.models.async_tasks import BulkUploadFileStatus
        if not own_hashes or getattr(job_file, "status", None) != BulkUploadFileStatus.SKIPPED_DUPLICATE:
            return False
        try:
            content = self.storage_service.download_file(_RESUME_STORAGE_BUCKET, job_file.storage_path)
        except Exception as exc:
            logger.warning("Erasure: a skipped duplicate file could not be read (%s) - left in place.",
                           type(exc).__name__)
            return False
        return hashlib.md5(content).hexdigest() in own_hashes

    def _erase_processing_and_cache_traces(self, resume_ids: list[UUID], parse_task_ids=()) -> dict:
        """
        Rows keyed to the resumes without a foreign key: their processing-stage
        rows (and those tasks' stage-failure rows), and the parse cache's LLM
        extraction of their text - found by the text hashes each parse recorded
        on the resume (resumes.parse_text_hashes) and those its derived rows
        recorded. Does not commit.

        Review item 13: also the stage and failure rows of their own parse tasks (parse_task_ids),
        linked to the resume or not - a failed parse's rows never were, and their error text can
        quote the resume.
        """
        from app.models.async_tasks import DocumentType
        stage_rows = cache_rows = 0
        if self.skill_detection_repo is not None:
            hashes = self.skill_detection_repo.resume_text_hashes(resume_ids)
            cache_rows = self.skill_detection_repo.delete_parse_cache_by_text_hashes(hashes)
        if self.document_processing_repo is not None:
            stage_rows = self.document_processing_repo.delete_for_documents(DocumentType.RESUME, resume_ids)
            stage_rows += self.document_processing_repo.delete_for_tasks(sorted(parse_task_ids or ()))
        return {"processing_stage_rows_deleted": stage_rows, "parse_cache_entries_deleted": cache_rows}

    def _strip_zip(self, job_id: UUID, mine: list, others: list) -> str:
        """
        Removes the candidate's entries from one job's stored ZIP: rebuilt under a new path when other
        files remain, deleted when none do. The ZIP is only read at extraction, so neither breaks the job.

        Review item 15: an entry is found by the position extraction staged it under ("0003_cv.pdf" is
        the fourth real entry). A file staged before positions were used ("{uuid}_cv.pdf") is matched by
        name only when no other file in the job has that name; otherwise the ZIP is left as it is and
        counted as a failure - never strip someone else's file. Storage errors are logged, never block
        the DB erasure.
        """
        if self.bulk_upload_job_repo is None or not mine:
            return "none"
        job = self.bulk_upload_job_repo.get_by_id(job_id)
        if job is None or not job.zip_storage_path:
            return "none"
        from app.services.resume.bulk_zip_entries import real_entries, staged_entry_index
        old_path = job.zip_storage_path
        try:
            archive = zipfile.ZipFile(io.BytesIO(self.storage_service.download_file(_RESUME_STORAGE_BUCKET, old_path)))
            entries = real_entries(archive)
            remove: set[str] = set()
            other_names = {f.original_filename for f in others}
            for job_file in mine:
                index = staged_entry_index(job_file.storage_path)
                if index is not None:
                    if index >= len(entries) or entries[index][1] != job_file.original_filename:
                        raise ValueError("the stored ZIP no longer matches its staged files")
                    remove.add(entries[index][0].filename)
                    continue
                same_name = [entry for entry, name in entries if name == job_file.original_filename]
                if len(same_name) != 1 or job_file.original_filename in other_names:
                    raise ValueError("an older staged file can't be told apart by name")
                remove.add(same_name[0].filename)
            kept = [entry for entry in archive.infolist() if entry.filename not in remove]
            if len(kept) == len(archive.infolist()):
                return "none"
            if not any(not entry.is_dir() for entry in kept):
                self._delete_resume_file(old_path)              # after the commit (item 14)
                job.zip_storage_path = None
                return "deleted"
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as rebuilt:
                for entry in kept:
                    rebuilt.writestr(entry, archive.read(entry))
            new_path = f"{old_path.rsplit('/', 1)[0]}/{uuid4()}.zip" if "/" in old_path else f"{uuid4()}.zip"
            self.storage_service.upload_file(bucket_name=_RESUME_STORAGE_BUCKET, file_path=new_path,
                                             file_content=buffer.getvalue(), content_type="application/zip")
            self._uploaded_this_run.append(new_path)
            job.zip_storage_path = new_path
            self._delete_resume_file(old_path)                  # after the commit (item 14)
            return "rebuilt"
        except Exception as exc:
            logger.warning("Erasure: could not remove the candidate's files from bulk ZIP of job %s (%s: %s)",
                           job_id, type(exc).__name__, exc if isinstance(exc, ValueError) else "storage error")
            return "failed"

    def _delete_resume_file(self, file_path: str) -> None:
        """
        Queues a stored file for deletion once the erasure has committed (review item 14): deleting it
        earlier lost the file when a later step rolled the erasure back. A delete that fails then is
        retried in the background and reported - never silently dropped.
        """
        if file_path:
            self._files_after_commit.append(file_path)
