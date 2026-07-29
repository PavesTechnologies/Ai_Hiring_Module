from datetime import datetime
from uuid import UUID

from app.exceptions.campaign_exceptions import CampaignException
from app.models.async_tasks import BulkUploadJob, BulkUploadStatus
from app.models.candidates import ParseStatus, Resume
from app.models.pipeline import PipelineStage
from app.repositories.bulk_upload_job_repository import BulkUploadJobRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.resume_repository import ResumeRepository
from app.schemas.upload_history.response import (
    UnifiedUploadHistoryEntry,
    UnifiedUploadHistoryResponse,
    UploaderOption,
)

# Epic 4 (M05-E04) Phase D7 - normalizes ParseStatus (individual) into the
# same outcome vocabulary bulk rows use. PARSING has no BulkUploadStatus
# analog and is kept as-is; DUPLICATE/PARTIAL_FAILURE/CANCELLED have no
# individual-row analog (see the plan/log for why - no durable duplicate
# flag exists on Resume) and so never appear on an "individual" entry.
_INDIVIDUAL_OUTCOME_MAP = {
    ParseStatus.PENDING: "PENDING",
    ParseStatus.PARSING: "PARSING",
    ParseStatus.PARSED: "COMPLETED",
    ParseStatus.FAILED: "FAILED",
}

_BULK_OUTCOME_MAP = {
    BulkUploadStatus.PENDING: "PENDING",
    BulkUploadStatus.EXTRACTING: "PROCESSING",
    BulkUploadStatus.PROCESSING: "PROCESSING",
    BulkUploadStatus.COMPLETED: "COMPLETED",
    BulkUploadStatus.PARTIAL_FAILURE: "PARTIAL_FAILURE",
    BulkUploadStatus.FAILED: "FAILED",
    BulkUploadStatus.CANCELLED: "CANCELLED",
}


class UploadHistoryService:
    """
    Epic 4 (M05-E04) Phase D7 — read-only aggregation of individual +
    bulk uploads for one campaign into a single, filterable, chronological
    view. No writes, no audit logging (export/audit-logging is D8's job).
    """

    def __init__(
        self,
        campaign_repo: CampaignRepository,
        resume_repo: ResumeRepository,
        bulk_upload_job_repo: BulkUploadJobRepository,
    ):
        self.campaign_repo = campaign_repo
        self.resume_repo = resume_repo
        self.bulk_upload_job_repo = bulk_upload_job_repo

    def get_history(
        self,
        campaign_id: UUID,
        *,
        uploaded_by: str | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        upload_type: str | None = None,
        outcome: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> UnifiedUploadHistoryResponse:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)

        individual_rows = self.resume_repo.get_campaign_history_entries(campaign_id)
        bulk_jobs = self.bulk_upload_job_repo.get_all_by_campaign(campaign_id)

        # One batched user_id -> full_name lookup for every uploader across
        # both sources, instead of a per-row query.
        uploader_ids = {resume.uploaded_by for resume, _ in individual_rows} | {
            job.uploaded_by for job in bulk_jobs
        }
        uploader_names = self.campaign_repo.get_hiring_manager_names(list(uploader_ids))

        all_entries = [
            self._to_individual_entry(resume, pipeline_stage, uploader_names)
            for resume, pipeline_stage in individual_rows
        ] + [self._to_bulk_entry(job, uploader_names) for job in bulk_jobs]

        # Derived from the FULL, unfiltered set - the dropdown always lists
        # every real uploader for this campaign, never shrinking as other
        # filters are applied.
        available_uploaders = self._build_uploader_options(all_entries)

        filtered = [
            e for e in all_entries
            if self._matches_filters(e, uploaded_by, date_from, date_to, upload_type, outcome)
        ]
        filtered.sort(key=lambda e: e.created_at, reverse=True)

        total = len(filtered)
        page = filtered[offset:offset + limit]

        return UnifiedUploadHistoryResponse(
            entries=page,
            total=total,
            limit=limit,
            offset=offset,
            available_uploaders=available_uploaders,
        )

    @staticmethod
    def _matches_filters(
        entry: UnifiedUploadHistoryEntry,
        uploaded_by: str | None,
        date_from: datetime | None,
        date_to: datetime | None,
        upload_type: str | None,
        outcome: str | None,
    ) -> bool:
        if uploaded_by is not None and entry.uploaded_by != uploaded_by:
            return False
        if date_from is not None and entry.created_at < date_from:
            return False
        if date_to is not None and entry.created_at > date_to:
            return False
        if upload_type is not None and entry.upload_type != upload_type:
            return False
        if outcome is not None and entry.outcome != outcome:
            return False
        return True

    @staticmethod
    def _build_uploader_options(entries: list[UnifiedUploadHistoryEntry]) -> list[UploaderOption]:
        seen: dict[str, str | None] = {}
        for entry in entries:
            if entry.uploaded_by not in seen:
                seen[entry.uploaded_by] = entry.uploaded_by_name
        return [UploaderOption(user_id=uid, full_name=name) for uid, name in seen.items()]

    @staticmethod
    def _to_individual_entry(
        resume: Resume,
        pipeline_stage: PipelineStage | None,
        uploader_names: dict[str, str],
    ) -> UnifiedUploadHistoryEntry:
        return UnifiedUploadHistoryEntry(
            upload_type="individual",
            filename=None,
            uploaded_by=resume.uploaded_by,
            uploaded_by_name=uploader_names.get(resume.uploaded_by),
            created_at=resume.created_at,
            outcome=_INDIVIDUAL_OUTCOME_MAP.get(resume.parse_status, resume.parse_status.value),
            resume_id=resume.id,
            parse_status=resume.parse_status,
            pipeline_stage=pipeline_stage,
        )

    @staticmethod
    def _to_bulk_entry(job: BulkUploadJob, uploader_names: dict[str, str]) -> UnifiedUploadHistoryEntry:
        outcome = _BULK_OUTCOME_MAP.get(job.status, job.status.value)
        if job.status not in (BulkUploadStatus.FAILED, BulkUploadStatus.CANCELLED) and job.duplicate_count > 0:
            # A job can be COMPLETED/PARTIAL_FAILURE and still have
            # duplicates - DUPLICATE only overrides the mapped outcome when
            # the job wasn't itself a hard failure, matching the backlog's
            # "All Duplicates" outcome value (bulk-only - see plan/log).
            outcome = "DUPLICATE"

        return UnifiedUploadHistoryEntry(
            upload_type="bulk",
            filename=job.original_filename,
            uploaded_by=job.uploaded_by,
            uploaded_by_name=uploader_names.get(job.uploaded_by),
            created_at=job.created_at,
            outcome=outcome,
            bulk_upload_job_id=job.id,
            total_files=job.total_files,
            processed_count=job.processed_count,
            failed_count=job.failed_count,
            duplicate_count=job.duplicate_count,
            status=job.status.value,
        )
