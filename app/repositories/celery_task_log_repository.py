from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.models.pipeline import CampaignCandidate


class CeleryTaskLogRepository:

    def __init__(self, db: Session):
        self.db = db

    def create(self, log: CeleryTaskLog):

        self.db.add(log)
        self.db.flush()
        self.db.refresh(log)

        return log

    def create_if_new_idempotency_key(self, log: CeleryTaskLog) -> tuple[CeleryTaskLog, bool]:
        """
        Same insert as create(), for callers whose idempotency_key can race
        under concurrent workers (e.g. EMBED_RESUME's enqueue helper) -
        uq_celery_task_log_idempotency_key (partial, WHERE idempotency_key
        IS NOT NULL) backs this at the DB level. A SAVEPOINT scopes a
        losing insert's IntegrityError to just this attempt (mirrors
        CandidateRepository.create's pattern for the same class of race),
        then falls back to the row the winner already committed instead of
        raising. Returns (log, was_created) - the caller must skip its own
        post-insert side effect (e.g. apply_async) when was_created is
        False, since that already happened for the winner's row.
        """
        try:
            with self.db.begin_nested():
                self.db.add(log)
                self.db.flush()
            self.db.refresh(log)
            return log, True
        except IntegrityError:
            existing = self.get_by_idempotency_key(log.idempotency_key)
            return existing, False

    def update(self, log: CeleryTaskLog):

        self.db.flush()
        self.db.refresh(log)

        return log

    def get_queued_dispatch_failed(self, task_type: str) -> list[CeleryTaskLog]:
        """
        Resume-upload resilience: rows whose apply_async() call itself
        failed at enqueue time (dispatch_failed=True) - never rows that
        were successfully queued and are simply waiting for a worker,
        which must never be redispatched (process_resume_document has no
        SUCCESS-shortcut, so a duplicate dispatch would reprocess the same
        resume twice).
        """
        return (
            self.db.query(CeleryTaskLog)
            .filter(
                CeleryTaskLog.task_type == task_type,
                CeleryTaskLog.status == TaskStatus.QUEUED,
                CeleryTaskLog.dispatch_failed.is_(True),
            )
            .all()
        )

    def claim_for_redispatch(self, task_log_id: UUID) -> bool:
        """
        Atomic compare-and-swap (single UPDATE...WHERE, not a
        read-then-write) - the caller may only call apply_async if this
        returns True. Prevents two concurrent recovery runs (e.g. the
        startup scan racing a Beat tick, or two app instances starting at
        once) from redispatching the same row twice.
        """
        result = self.db.execute(
            update(CeleryTaskLog)
            .where(CeleryTaskLog.id == task_log_id, CeleryTaskLog.dispatch_failed.is_(True))
            .values(dispatch_failed=False),
        )
        self.db.commit()
        return result.rowcount == 1

    def get_by_task_id(self, task_id: str) -> CeleryTaskLog | None:
        return (
            self.db.query(CeleryTaskLog)
            .filter(CeleryTaskLog.task_id == task_id)
            .first()
        )

    def get_by_idempotency_key(self, idempotency_key: str) -> CeleryTaskLog | None:
        """
        uq_celery_task_log_idempotency_key (partial, WHERE idempotency_key
        IS NOT NULL) backs uniqueness at the DB level for callers that
        insert via create_if_new_idempotency_key(). Callers still using
        plain create() get no such guarantee and must rely on this
        pre-check alone to avoid a duplicate.
        """
        return (
            self.db.query(CeleryTaskLog)
            .filter(CeleryTaskLog.idempotency_key == idempotency_key)
            .first()
        )

    def get_by_campaign_candidate_and_task_type(
        self, campaign_candidate_id: UUID, task_type: str,
    ) -> list[CeleryTaskLog]:
        """
        M07-E03 S01 T03: every task log of a given type queued/run for one
        campaign_candidate - scoped strictly to this id (never other
        candidates/campaigns). Caller filters by status (e.g. QUEUED only)
        since different callers care about different statuses.
        """
        return (
            self.db.query(CeleryTaskLog)
            .filter(
                CeleryTaskLog.campaign_candidate_id == campaign_candidate_id,
                CeleryTaskLog.task_type == task_type,
            )
            .all()
        )

    def get_earliest_started_at_by_bulk_upload_job_id(self, bulk_upload_job_id: UUID) -> datetime | None:
        """
        Epic 4 (M05-E04) Phase D4 — the earliest real started_at across
        every task tied to this bulk job (BULK_EXTRACT and/or per-file
        parse tasks), used as a more accurate "processing began" signal
        for ETA math than bulk_upload_jobs.created_at (job-row-insertion
        time, before any worker has actually picked anything up).
        """
        stmt = select(func.min(CeleryTaskLog.started_at)).where(
            CeleryTaskLog.bulk_upload_job_id == bulk_upload_job_id,
            CeleryTaskLog.started_at.is_not(None),
        )
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> list[CeleryTaskLog]:
        """
        Epic 4 (M05-E04) Phase D2 — every task log of every type for one
        campaign_candidate, oldest first, backing the scorecard's
        Processing Timeline. Broader than
        get_by_campaign_candidate_and_task_type (no task_type filter) —
        both RESUME_DOCUMENT_PROCESSING and DETERMINISTIC_SCORE already
        set campaign_candidate_id, so this surfaces the real multi-stage
        history without needing a task_type allowlist.
        """
        return (
            self.db.query(CeleryTaskLog)
            .filter(CeleryTaskLog.campaign_candidate_id == campaign_candidate_id)
            .order_by(CeleryTaskLog.queued_at.asc())
            .all()
        )

    def get_by_task_ids(self, task_ids: list[str]) -> list[CeleryTaskLog]:
        """
        Batched counterpart to get_by_task_id — one query for a whole
        job's worth of per-file tasks instead of one query per file.
        Caller keys the result by task_id.
        """
        if not task_ids:
            return []
        return (
            self.db.query(CeleryTaskLog)
            .filter(CeleryTaskLog.task_id.in_(task_ids))
            .all()
        )

    def get_recent_by_created_by(self, created_by: str, limit: int = 50) -> list[CeleryTaskLog]:
        """
        Excludes SUCCESS: this backs the "my uploads" list, which only
        needs to surface uploads still in flight or that need attention —
        a fully successful upload already shows up as a real JD in the
        normal JD list, so repeating it here would be noise.
        """
        return (
            self.db.query(CeleryTaskLog)
            .filter(
                CeleryTaskLog.created_by == created_by,
                CeleryTaskLog.status != TaskStatus.SUCCESS,
            )
            .order_by(CeleryTaskLog.queued_at.desc())
            .limit(limit)
            .all()
        )

    def count_by_task_type_and_statuses(
        self,
        task_type: str,
        statuses: list[TaskStatus],
        campaign_id: UUID | None = None,
    ) -> int:
        """
        Monitoring-only. campaign_id scopes to that campaign's own
        campaign_candidates rows via celery_task_log.campaign_candidate_id
        — set for individual-upload RESUME_DOCUMENT_PROCESSING tasks, so
        this only makes sense called with that task_type.
        """
        conditions = [
            CeleryTaskLog.task_type == task_type,
            CeleryTaskLog.status.in_(statuses),
        ]
        if campaign_id is not None:
            candidate_ids_in_campaign = select(CampaignCandidate.id).where(
                CampaignCandidate.campaign_id == campaign_id
            )
            conditions.append(CeleryTaskLog.campaign_candidate_id.in_(candidate_ids_in_campaign))
        stmt = select(func.count()).select_from(CeleryTaskLog).where(*conditions)
        return self.db.execute(stmt).scalar_one()

    def count_completed_since(self, since: datetime, task_types: list[str]) -> int:
        """Monitoring-only — backs processing-metrics' throughput_per_hour."""
        stmt = select(func.count()).select_from(CeleryTaskLog).where(
            CeleryTaskLog.task_type.in_(task_types),
            CeleryTaskLog.status == TaskStatus.SUCCESS,
            CeleryTaskLog.completed_at.is_not(None),
            CeleryTaskLog.completed_at >= since,
        )
        return self.db.execute(stmt).scalar_one()

    def delete_by_resume_id(self, resume_id: UUID) -> None:
        """Candidate erasure — removes celery_task_log rows tied to one resume version."""
        self.db.execute(delete(CeleryTaskLog).where(CeleryTaskLog.resume_id == resume_id))
        self.db.flush()

    def delete_by_campaign_candidate_id(self, campaign_candidate_id: UUID) -> None:
        """Candidate erasure — removes celery_task_log rows tied to one campaign_candidate."""
        self.db.execute(delete(CeleryTaskLog).where(CeleryTaskLog.campaign_candidate_id == campaign_candidate_id))
        self.db.flush()

    def delete_by_task_id(self, task_id: str) -> None:
        """
        Dead-letter cleanup (orphaned-failure path) — removes one
        celery_task_log row directly. Only safe when no dead_letter_queue
        row references this task_id (that FK would otherwise block the
        delete) - DeadLetterCleanupService checks that before calling this.
        """
        self.db.execute(delete(CeleryTaskLog).where(CeleryTaskLog.task_id == task_id))
        self.db.flush()

    def commit(self):
        self.db.commit()

    def rollback(self):
        self.db.rollback()