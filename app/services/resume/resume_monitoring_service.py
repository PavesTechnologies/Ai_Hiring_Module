import logging
from collections import Counter
from datetime import datetime
from uuid import UUID

from app.core.encryption_service import DecryptionError, EncryptionService
from app.core.storage_service import StorageService
from app.exceptions.storage_exception import StorageException
from app.exception_handler.exceptions import BadRequestError, NotFoundError
from app.models.candidates import ParseStatus
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.candidate_repository import CandidateRepository
from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
from app.repositories.dead_letter_queue_repository import DeadLetterQueueRepository
from app.repositories.document_processing_repository import DocumentProcessingRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.resume_repository import ResumeRepository
from app.repositories.stage_failure_log_repository import StageFailureLogRepository
from app.repositories.user_repository import UserRepository
from app.core.config import settings
from app.core.cache_keys import resume_key, resume_list_key
from app.services.cache_service import CacheService
from app.schemas.resume.monitoring import (
    CandidateSummary,
    EmbeddingStatus,
    ParseAttemptItem,
    ParserInfo,
    ProcessingSummary,
    ResumeDetailResponse,
    ResumeListItem,
    ResumeListItemWithPipeline,
    ResumeListResponse,
    ResumeListWithPipelineResponse,
    ResumeParsedJsonResponse,
    ResumeSummary,
    ResumeTimelineResponse,
    SkillSummary,
)
from app.schemas.resume.response import (
    EducationComparison,
    EducationEntryComparison,
    ExperienceComparison,
    ExperienceEntryComparison,
    ExperienceYearsComparison,
    ResumeComparisonSummary,
    ResumeDownloadUrlResponse,
    ResumeVersionCampaignUsage,
    ResumeVersionComparisonResponse,
    ResumeVersionHistoryResponse,
    ResumeVersionItem,
    ResumeVersionSnapshot,
    SkillsComparison,
)
from app.services.resume.monitoring_shared import build_failure_info, build_stage_timeline_fields
from app.services.resume.resume_version_diff import (
    diff_education,
    diff_experience,
    diff_experience_years,
    diff_skills,
)
from app.services.resume.work_experience_duration import annotate_work_experience_durations

logger = logging.getLogger(__name__)

CANDIDATE_PII_PURPOSE = "CANDIDATE_PII"
UNDECRYPTABLE_PLACEHOLDER = "[undecryptable]"
RESUME_STORAGE_BUCKET = "airs_resumes"
DOWNLOAD_URL_EXPIRES_IN_SECONDS = 900

# S02-T01 - config-driven expiry for the per-version resume download URL,
# distinct from DOWNLOAD_URL_EXPIRES_IN_SECONDS above (which backs the
# unrelated parsed-json-by-campaign-candidate endpoint and stays a fixed
# constant per its own story).
RESUME_DOWNLOAD_URL_EXPIRY_SECONDS_KEY = "RESUME_DOWNLOAD_URL_EXPIRY_SECONDS"
DEFAULT_RESUME_DOWNLOAD_URL_EXPIRY_SECONDS = 300


class ResumeMonitoringService:
    """
    Read-only monitoring/tracking service for the frontend UI — does not
    write to any table the processing pipeline owns. Deliberately separate
    from ResumeProcessingStatusService (which backs the existing production
    GET /resumes/processing-status/{task_id} endpoint) rather than an
    extension of it, so that endpoint's response shape stays untouched.
    """

    def __init__(
        self,
        resume_repository: ResumeRepository,
        candidate_repository: CandidateRepository,
        encryption_service: EncryptionService,
        task_log_repository: CeleryTaskLogRepository,
        stage_repository: DocumentProcessingRepository,
        stage_failure_log_repository: StageFailureLogRepository,
        dead_letter_queue_repository: DeadLetterQueueRepository,
        storage_service: StorageService,
        campaign_candidate_repository: CampaignCandidateRepository,
        user_repository: UserRepository | None = None,
        config_repository: ConfigRepository | None = None,
        cache_service: CacheService | None = None,
    ):
        self.resume_repository = resume_repository
        self.candidate_repository = candidate_repository
        self.encryption_service = encryption_service
        self.task_log_repository = task_log_repository
        self.stage_repository = stage_repository
        self.stage_failure_log_repository = stage_failure_log_repository
        self.dead_letter_queue_repository = dead_letter_queue_repository
        self.storage_service = storage_service
        self.campaign_candidate_repository = campaign_candidate_repository
        self.user_repository = user_repository
        self.config_repository = config_repository
        self.cache_service = cache_service

    def get_timeline(self, resume_id: UUID, attempt_number: int | None = None) -> ResumeTimelineResponse:
        """
        attempt_number is optional and defaults to the current/latest
        attempt. This matters because a genuine Celery retry re-runs every
        stage from TEXT_EXTRACTION again — nothing is skipped on a real
        retry (initial_context is never passed for individual upload) — so
        document_processing_stage_executions accumulates a full set of 7
        rows per attempt, not just for the stage that failed. Without
        filtering to one attempt, a retried resume would show duplicate
        entries for the same stage (e.g. two AI_EXTRACTION rows, one FAILED
        at attempt 1 and one SUCCESS at attempt 2) instead of one clean
        7-stage timeline.
        """
        resume = self._get_resume_or_404(resume_id)
        task_id = self._require_task_id(resume)

        task_log = self.task_log_repository.get_by_task_id(task_id)
        if task_log is None:
            raise NotFoundError(f"No task log found for resume {resume_id}.")

        executions = self.stage_repository.get_by_task_id(task_id)
        failure_logs = self.stage_failure_log_repository.get_by_task_id(task_id)

        fields = build_stage_timeline_fields(task_id, task_log, executions, failure_logs, attempt_number)
        return ResumeTimelineResponse(resume_id=resume.id, **fields)

    def get_parse_attempts(self, resume_id: UUID) -> list[ParseAttemptItem]:
        resume = self._get_resume_or_404(resume_id)

        items = [
            ParseAttemptItem(
                source="parse_attempt",
                attempt_number=attempt.attempt_number,
                stage=None,
                parser_used=attempt.parser_used,
                parser_version=attempt.parser_version,
                status=attempt.status.value,
                error_code=attempt.error_code,
                error_detail=attempt.error_detail,
                confidence_score=attempt.confidence_score,
                duration_ms=attempt.duration_ms,
                occurred_at=attempt.attempted_at,
            )
            for attempt in self.resume_repository.get_parse_attempts(resume.id)
        ]

        if resume.task_id:
            # resume_parse_attempts only ever records a *successful* attempt
            # (see docs/Resume_Intake_Monitoring_API_Design.md, Gap 2) — a
            # resume that failed before ever reaching PERSISTENCE has zero
            # rows there. stage_failure_logs is where that history actually
            # lives, so it's merged in here rather than silently omitted.
            items.extend(
                ParseAttemptItem(
                    source="stage_failure",
                    attempt_number=failure.attempt_number,
                    stage=failure.stage.value,
                    parser_used=None,
                    parser_version=None,
                    status=failure.classification.value,
                    error_code=failure.exception_type,
                    error_detail=failure.message,
                    confidence_score=None,
                    duration_ms=None,
                    occurred_at=failure.created_at,
                )
                for failure in self.stage_failure_log_repository.get_by_task_id(resume.task_id)
            )

        items.sort(key=lambda item: item.occurred_at)
        return items

    def get_resume_detail(self, resume_id: UUID) -> ResumeDetailResponse:
        if not self.cache_service:
            return self._load_resume_detail(resume_id)

        raw = self.cache_service.get_or_set(
            resume_key(resume_id),
            loader=lambda: self._load_resume_detail(resume_id).model_dump_json(),
            ttl=settings.cache_resume_ttl_seconds,
        )
        return ResumeDetailResponse.model_validate_json(raw)

    def _load_resume_detail(self, resume_id: UUID) -> ResumeDetailResponse:
        resume = self._get_resume_or_404(resume_id)

        candidate = self.candidate_repository.get_by_id(resume.candidate_id)
        if candidate is None:
            raise NotFoundError(f"Candidate for resume {resume_id} not found.")

        full_name = self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
        email = self.encryption_service.decrypt(candidate.email_encrypted, candidate.encryption_key_id)

        task_log = self.task_log_repository.get_by_task_id(resume.task_id) if resume.task_id else None
        executions = self.stage_repository.get_by_task_id(resume.task_id) if resume.task_id else []

        skills = self.resume_repository.get_candidate_skills(resume.id)
        skill_summary = SkillSummary(
            total_skills=len(skills),
            matched=sum(1 for skill in skills if skill.canonical_skill_id is not None),
            unmatched=sum(1 for skill in skills if skill.canonical_skill_id is None),
            by_tier=dict(Counter(skill.match_tier for skill in skills)),
        )

        embedding = self.resume_repository.get_embedding(resume.id)
        embedding_status = EmbeddingStatus(
            exists=embedding is not None,
            embedding_model_version_id=embedding.embedding_model_version_id if embedding else None,
            generated_at=embedding.created_at if embedding else None,
        )

        # parser_used isn't a column on Resume itself — only recorded per
        # attempt in resume_parse_attempts — so pull it from the most
        # recent attempt rather than leaving it always null.
        parse_attempts = self.resume_repository.get_parse_attempts(resume.id)
        parser_used = parse_attempts[-1].parser_used if parse_attempts else None

        failure = None
        if resume.parse_status == ParseStatus.FAILED:
            failure_logs = self.stage_failure_log_repository.get_by_task_id(resume.task_id) if resume.task_id else []
            dlq_entry = self.dead_letter_queue_repository.get_by_task_id(resume.task_id) if resume.task_id else None
            failure = build_failure_info(executions, failure_logs, dlq_entry, task_log)

        return ResumeDetailResponse(
            resume=ResumeSummary(
                id=resume.id,
                file_path=resume.file_path,
                file_format=resume.file_format.value,
                version_number=resume.version_number,
                is_active_version=resume.is_active_version,
                parse_status=resume.parse_status.value,
                parser_version=resume.parser_version,
                page_count=resume.page_count,
                created_at=resume.created_at,
                bulk_upload_job_id=resume.bulk_upload_job_id,
            ),
            candidate=CandidateSummary(
                id=candidate.id,
                full_name=full_name,
                email=email,
                jurisdiction=candidate.jurisdiction,
                consent_given=candidate.consent_given,
            ),
            processing=ProcessingSummary(
                task_id=resume.task_id,
                current_status=task_log.status.value if task_log else None,
                current_stage=executions[-1].stage.value if executions else None,
                attempt_number=(task_log.retry_count + 1) if task_log else None,
                retry_count=task_log.retry_count if task_log else None,
            ),
            skill_summary=skill_summary,
            embedding_status=embedding_status,
            parser_info=ParserInfo(parser_used=parser_used, parser_version=resume.parser_version),
            failure=failure,
        )

    def _fetch_resume_page(
        self,
        *,
        campaign_id: UUID | None,
        parse_status: ParseStatus | None,
        source: str | None,
        email_hash: str | None,
        uploaded_from: datetime | None,
        uploaded_to: datetime | None,
        page: int,
        size: int,
        sort_by: str,
        sort_dir: str,
    ):
        """
        Shared page-fetch for list_resumes/list_resumes_with_pipeline_status -
        same filters/pagination/batched lookups, so the two response shapes
        never drift out of sync on what counts as "the page". Returns
        (resumes, total, candidates_by_id, campaign_candidates_by_resume_id)
        where campaign_candidates_by_resume_id holds the full
        CampaignCandidate row (not just its id) so a caller can read
        pipeline_stage/decision_* off it without a second query.
        """
        filters = dict(
            campaign_id=campaign_id,
            parse_status=parse_status,
            source=source,
            email_hash=email_hash,
            uploaded_from=uploaded_from,
            uploaded_to=uploaded_to,
        )
        resumes = self.resume_repository.search(
            **filters, page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )
        total = self.resume_repository.count_search(**filters)

        # One batched candidate lookup for the whole page, not one query per row.
        candidates_by_id = {
            candidate.id: candidate
            for candidate in self.candidate_repository.get_by_ids([r.candidate_id for r in resumes])
        }

        # Same batching for campaign_candidates. A resume reused across
        # campaigns ("use existing" duplicate resolution) can have more
        # than one row here; when campaign_id is part of this search's own
        # filters it's already an exact, unambiguous match. When it isn't,
        # the most recently created link is shown as a best-effort pick —
        # there's no single "correct" one to prefer without a campaign in
        # scope.
        campaign_candidates_by_resume_id = {}
        for cc in sorted(
            self.campaign_candidate_repository.get_by_resume_ids(
                [r.id for r in resumes], campaign_id=campaign_id,
            ),
            key=lambda cc: cc.created_at,
        ):
            campaign_candidates_by_resume_id[cc.resume_id] = cc

        return resumes, total, candidates_by_id, campaign_candidates_by_resume_id

    def _decrypt_candidate_identity(self, candidate) -> tuple[str, str]:
        if candidate is None:
            return "Unknown", "Unknown"
        full_name = self._safe_decrypt(candidate.full_name_encrypted, candidate.encryption_key_id, candidate.id)
        email = self._safe_decrypt(candidate.email_encrypted, candidate.encryption_key_id, candidate.id)
        return full_name, email

    def list_resumes(
        self,
        *,
        campaign_id: UUID | None = None,
        parse_status: ParseStatus | None = None,
        source: str | None = None,
        email_hash: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> ResumeListResponse:
        params = dict(
            campaign_id=campaign_id, parse_status=parse_status, source=source, email_hash=email_hash,
            uploaded_from=uploaded_from, uploaded_to=uploaded_to,
            page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )

        if not self.cache_service:
            return self._load_resume_list(**params)

        raw = self.cache_service.get_or_set(
            resume_list_key(params),
            loader=lambda: self._load_resume_list(**params).model_dump_json(),
            ttl=settings.cache_resume_list_ttl_seconds,
        )
        return ResumeListResponse.model_validate_json(raw)

    def _load_resume_list(
        self,
        *,
        campaign_id: UUID | None,
        parse_status: ParseStatus | None,
        source: str | None,
        email_hash: str | None,
        uploaded_from: datetime | None,
        uploaded_to: datetime | None,
        page: int,
        size: int,
        sort_by: str,
        sort_dir: str,
    ) -> ResumeListResponse:
        resumes, total, candidates_by_id, campaign_candidates_by_resume_id = self._fetch_resume_page(
            campaign_id=campaign_id, parse_status=parse_status, source=source, email_hash=email_hash,
            uploaded_from=uploaded_from, uploaded_to=uploaded_to,
            page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )

        items = []
        for resume in resumes:
            full_name, email = self._decrypt_candidate_identity(candidates_by_id.get(resume.candidate_id))
            cc = campaign_candidates_by_resume_id.get(resume.id)
            items.append(
                ResumeListItem(
                    id=resume.id,
                    resume_id=resume.id,
                    task_id=resume.task_id,
                    candidate_id=resume.candidate_id,
                    campaign_candidate_id=cc.id if cc else None,
                    candidate_full_name=full_name,
                    candidate_email=email,
                    file_format=resume.file_format.value,
                    parse_status=resume.parse_status.value,
                    version_number=resume.version_number,
                    is_active_version=resume.is_active_version,
                    source="bulk" if resume.bulk_upload_job_id else "individual",
                    bulk_upload_job_id=resume.bulk_upload_job_id,
                    created_at=resume.created_at,
                )
            )

        return ResumeListResponse(items=items, total=total, page=page, size=size)

    def list_resumes_with_pipeline_status(
        self,
        *,
        campaign_id: UUID | None = None,
        parse_status: ParseStatus | None = None,
        source: str | None = None,
        email_hash: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
    ) -> ResumeListWithPipelineResponse:
        """
        Same rows as list_resumes, plus each resume's linked
        campaign_candidate pipeline_stage/decision_* fields - lets the
        frontend show, for each resume, which stage the candidate is on
        and whether they succeeded/failed there, without a second
        per-candidate call.
        """
        params = dict(
            kind="pipeline", campaign_id=campaign_id, parse_status=parse_status, source=source,
            email_hash=email_hash, uploaded_from=uploaded_from, uploaded_to=uploaded_to,
            page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )

        if not self.cache_service:
            return self._load_resume_list_with_pipeline_status(
                campaign_id=campaign_id, parse_status=parse_status, source=source, email_hash=email_hash,
                uploaded_from=uploaded_from, uploaded_to=uploaded_to,
                page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
            )

        raw = self.cache_service.get_or_set(
            resume_list_key(params),
            loader=lambda: self._load_resume_list_with_pipeline_status(
                campaign_id=campaign_id, parse_status=parse_status, source=source, email_hash=email_hash,
                uploaded_from=uploaded_from, uploaded_to=uploaded_to,
                page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
            ).model_dump_json(),
            ttl=settings.cache_resume_list_ttl_seconds,
        )
        return ResumeListWithPipelineResponse.model_validate_json(raw)

    def _load_resume_list_with_pipeline_status(
        self,
        *,
        campaign_id: UUID | None,
        parse_status: ParseStatus | None,
        source: str | None,
        email_hash: str | None,
        uploaded_from: datetime | None,
        uploaded_to: datetime | None,
        page: int,
        size: int,
        sort_by: str,
        sort_dir: str,
    ) -> ResumeListWithPipelineResponse:
        resumes, total, candidates_by_id, campaign_candidates_by_resume_id = self._fetch_resume_page(
            campaign_id=campaign_id, parse_status=parse_status, source=source, email_hash=email_hash,
            uploaded_from=uploaded_from, uploaded_to=uploaded_to,
            page=page, size=size, sort_by=sort_by, sort_dir=sort_dir,
        )

        items = []
        for resume in resumes:
            full_name, email = self._decrypt_candidate_identity(candidates_by_id.get(resume.candidate_id))
            cc = campaign_candidates_by_resume_id.get(resume.id)
            items.append(
                ResumeListItemWithPipeline(
                    id=resume.id,
                    resume_id=resume.id,
                    task_id=resume.task_id,
                    candidate_id=resume.candidate_id,
                    campaign_id=cc.campaign_id if cc else None,
                    campaign_candidate_id=cc.id if cc else None,
                    candidate_full_name=full_name,
                    candidate_email=email,
                    file_format=resume.file_format.value,
                    parse_status=resume.parse_status.value,
                    version_number=resume.version_number,
                    is_active_version=resume.is_active_version,
                    source="bulk" if resume.bulk_upload_job_id else "individual",
                    bulk_upload_job_id=resume.bulk_upload_job_id,
                    created_at=resume.created_at,
                    pipeline_stage=cc.pipeline_stage.value if cc else None,
                    decision_type=cc.decision_type.value if cc and cc.decision_type else None,
                    decision_source=cc.decision_source.value if cc and cc.decision_source else None,
                    decision_reason=cc.decision_reason if cc else None,
                    decision_at=cc.decision_at if cc else None,
                )
            )

        return ResumeListWithPipelineResponse(items=items, total=total, page=page, size=size)

    def get_parsed_json_by_campaign_candidate(self, campaign_candidate_id: UUID) -> ResumeParsedJsonResponse:
        campaign_candidate = self.campaign_candidate_repository.get_by_id(campaign_candidate_id)
        if campaign_candidate is None:
            raise NotFoundError(f"Campaign candidate {campaign_candidate_id} not found.")

        resume = self.resume_repository.get_by_id(campaign_candidate.resume_id)
        if resume is None or resume.candidate_id != campaign_candidate.candidate_id:
            raise NotFoundError(f"No resume found for campaign candidate {campaign_candidate_id}.")

        download_url = None
        try:
            download_url = self.storage_service.generate_signed_url(
                bucket_name=RESUME_STORAGE_BUCKET,
                file_path=resume.file_path,
                expires_in=DOWNLOAD_URL_EXPIRES_IN_SECONDS,
            )
        except StorageException:
            logger.exception(
                "Failed to generate download URL for resume_id=%s file_path=%s",
                resume.id, resume.file_path,
            )

        return ResumeParsedJsonResponse(
            resume_id=resume.id,
            candidate_id=resume.candidate_id,
            parse_status=resume.parse_status.value,
            parsed_json=annotate_work_experience_durations(resume.parsed_json),
            original_filename=resume.original_filename,
            file_format=resume.file_format.value,
            file_size_bytes=resume.file_size_bytes,
            page_count=resume.page_count,
            created_at=resume.created_at,
            updated_at=resume.updated_at,
            download_url=download_url,
        )

    def get_version_history(self, candidate_id: UUID) -> ResumeVersionHistoryResponse:
        """
        Epic 3 (M05-E03) Phase C1 — read-only, mirrors get_parsed_json_by_candidate's
        style of resolving by candidate_id directly rather than a separate
        existence check.

        S02-T01 extended this with per-version parse_confidence, uploaded_by,
        and the campaigns/pipeline-stages each version was used in - all
        batched (one query each for the whole version list, not per-row).
        """
        versions = self.resume_repository.get_all_versions_by_candidate(candidate_id)
        if not versions:
            raise NotFoundError(f"No resumes found for candidate {candidate_id}.")

        resume_ids = [resume.id for resume in versions]
        campaigns_by_resume_id: dict[UUID, list[ResumeVersionCampaignUsage]] = {}
        for resume_id, campaign_id, campaign_name, pipeline_stage in (
            self.campaign_candidate_repository.get_campaign_usage_by_resume_ids(resume_ids)
        ):
            campaigns_by_resume_id.setdefault(resume_id, []).append(
                ResumeVersionCampaignUsage(
                    campaign_id=campaign_id,
                    campaign_name=campaign_name,
                    pipeline_stage=pipeline_stage.value,
                )
            )

        uploaders_by_id = {
            user.id: user.full_name
            for user in self.user_repository.get_by_ids([resume.uploaded_by for resume in versions])
        }

        return ResumeVersionHistoryResponse(
            candidate_id=candidate_id,
            versions=[
                ResumeVersionItem(
                    id=resume.id,
                    version_number=resume.version_number,
                    is_active_version=resume.is_active_version,
                    file_format=resume.file_format.value,
                    parse_status=resume.parse_status.value,
                    parse_confidence=(
                        float(resume.parse_confidence_score)
                        if resume.parse_confidence_score is not None else None
                    ),
                    uploaded_by=uploaders_by_id.get(resume.uploaded_by, resume.uploaded_by),
                    source="bulk" if resume.bulk_upload_job_id else "individual",
                    created_at=resume.created_at,
                    campaigns=campaigns_by_resume_id.get(resume.id, []),
                )
                for resume in versions
            ],
        )

    def get_download_url(self, resume_id: UUID) -> ResumeDownloadUrlResponse:
        """
        S02-T01 — server-generated MinIO/Supabase signed URL for one
        specific resume version (not a campaign's "active" resume, and not
        routed through ResumeSelectionService — a plain resume_id lookup).
        Expiry is config-driven via RESUME_DOWNLOAD_URL_EXPIRY_SECONDS,
        defaulting to 300s when unset.
        """
        resume = self._get_resume_or_404(resume_id)
        expires_in = self._get_download_url_expiry_seconds()

        download_url = self.storage_service.generate_signed_url(
            bucket_name=RESUME_STORAGE_BUCKET,
            file_path=resume.file_path,
            expires_in=expires_in,
        )

        return ResumeDownloadUrlResponse(
            resume_id=resume.id,
            version_number=resume.version_number,
            download_url=download_url,
            expires_in_seconds=expires_in,
        )

    def _get_download_url_expiry_seconds(self) -> int:
        configured = self.config_repository.get_configs_by_keys(
            [RESUME_DOWNLOAD_URL_EXPIRY_SECONDS_KEY],
        ).get(RESUME_DOWNLOAD_URL_EXPIRY_SECONDS_KEY)
        return int(configured) if configured else DEFAULT_RESUME_DOWNLOAD_URL_EXPIRY_SECONDS

    def compare_resume_versions(
        self, resume_id_1: UUID, resume_id_2: UUID,
    ) -> ResumeVersionComparisonResponse:
        """
        S02-T02 — read-only diff of two resume versions' parsed_json,
        computed fresh from the two Resume rows on every call. Nothing is
        stored: no comparison/diff table or column exists, and this method
        never writes to the database. Diffs the raw stored parsed_json
        (not annotate_work_experience_durations' response-time-recomputed
        view used by get_parsed_json_by_campaign_candidate) so a
        total_experience_years difference always reflects a genuine change
        between the two stored versions, never a recomputation artifact.
        """
        if resume_id_1 == resume_id_2:
            raise BadRequestError("Select two different resume versions to compare.")

        resume_1 = self._get_resume_or_404(resume_id_1)
        resume_2 = self._get_resume_or_404(resume_id_2)
        if resume_1.candidate_id != resume_2.candidate_id:
            raise BadRequestError("Both resume versions must belong to the same candidate.")

        parsed_1 = resume_1.parsed_json or {}
        parsed_2 = resume_2.parsed_json or {}

        skills = diff_skills(parsed_1.get("skills") or [], parsed_2.get("skills") or [])
        experience = diff_experience(parsed_1.get("work_experience") or [], parsed_2.get("work_experience") or [])
        education = diff_education(parsed_1.get("education") or [], parsed_2.get("education") or [])
        experience_years = diff_experience_years(
            parsed_1.get("total_experience_years"), parsed_2.get("total_experience_years"),
        )

        return ResumeVersionComparisonResponse(
            candidate_id=resume_1.candidate_id,
            version_1=self._to_version_snapshot(resume_1),
            version_2=self._to_version_snapshot(resume_2),
            skills=SkillsComparison(**skills),
            experience=ExperienceComparison(
                added=[ExperienceEntryComparison(**entry) for entry in experience["added"]],
                removed=[ExperienceEntryComparison(**entry) for entry in experience["removed"]],
            ),
            education=EducationComparison(
                added=[EducationEntryComparison(**entry) for entry in education["added"]],
                removed=[EducationEntryComparison(**entry) for entry in education["removed"]],
            ),
            experience_years=ExperienceYearsComparison(**experience_years),
            summary=ResumeComparisonSummary(
                skills_added=len(skills["added"]),
                skills_removed=len(skills["removed"]),
                skills_unchanged=len(skills["unchanged"]),
                experience_years_change=experience_years["difference"],
            ),
        )

    @staticmethod
    def _to_version_snapshot(resume) -> ResumeVersionSnapshot:
        return ResumeVersionSnapshot(
            resume_id=resume.id,
            version_number=resume.version_number,
            parse_status=resume.parse_status.value,
            created_at=resume.created_at,
            parsed_json=resume.parsed_json or {},
        )

    def _safe_decrypt(self, ciphertext: bytes, encryption_key_id, candidate_id: UUID) -> str:
        """
        List view only — one candidate's PII becoming undecryptable (e.g. it
        was encrypted under a key value that no longer matches what's
        configured) must not 500 the whole paginated page of otherwise-fine
        rows. get_resume_detail intentionally still raises for a single-row
        lookup, where silently substituting a placeholder would be worse
        than a clear error.
        """
        try:
            return self.encryption_service.decrypt(ciphertext, encryption_key_id)
        except DecryptionError:
            logger.error("Candidate %s PII is undecryptable with the configured encryption key.", candidate_id)
            return UNDECRYPTABLE_PLACEHOLDER

    def _get_resume_or_404(self, resume_id: UUID):
        resume = self.resume_repository.get_by_id(resume_id)
        if resume is None:
            raise NotFoundError(f"Resume {resume_id} not found.")
        return resume

    @staticmethod
    def _require_task_id(resume) -> str:
        if not resume.task_id:
            raise NotFoundError(
                f"No processing task has been scheduled for resume {resume.id} yet."
            )
        return resume.task_id
