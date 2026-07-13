import io
import logging
import re
from dataclasses import dataclass
from datetime import timezone
from urllib import request
from uuid import UUID, uuid4

from docx import Document
from fastapi import HTTPException, UploadFile

from app.models.jd.job_descriptions import JobDescription, JDSourceFormat, JDVerificationStatus
from app.repositories.jd_repository import JDRepository
from app.repositories.skill_repository import SkillRepository
from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.services.skills.skill_normalization_service import SkillMatchResult, verification_status_for_tier
from app.schemas.jd.request import CreateJDRequest, UpdateJDRequest, JDSearchRequest
from app.schemas.jd.response import UpdateJDResponse, JDListItem, PaginatedJDResponse
from app.services.jd.hash_service import HashService
from app.services.audit_service import AuditService
from app.enums.constants import ActionType, EntityType
from app.schemas.jd.response import GetJDResponse
from app.exception_handler.exceptions import NotFoundError, BadRequestError, ConflictError
from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.schemas.jd.DuplicateJDInfo import DuplicateJDInfo, ExistingJDInfo
from app.mappers.jd_mapper import JDMapper
from app.core.storage_service import StorageService
from fastapi.responses import StreamingResponse
from datetime import datetime
from app.utils.excel_export import ExcelExport

logger = logging.getLogger(__name__)


@dataclass
class JDReprocessRequired:
    """
    Returned by JDService.update_jd() instead of UpdateJDResponse when
    raw_text or the JD's file changed — those are the only two update
    triggers the finalized design re-runs the pipeline for. JDService
    itself doesn't queue Celery tasks (that would import the task module,
    which imports JDService — circular); the router queues process_jd_document
    with these fields, exactly as it already does for JD creation.
    """
    existing_jd_id: UUID
    version_number: int
    parent_jd_id: UUID
    lineage_root_id: UUID
    title: str
    jurisdiction: str
    min_experience_years: float | None
    education_criteria: dict | None
    raw_text: str | None
    file_path: str | None
    old_file_path: str | None
    updated_by: str


class JDService:

    JD_STORAGE_BUCKET = "airs-job-descriptions"
    MAX_JD_EXPORT_RECORDS = 5000
    EXPORT_AUDIT_ENTITY_ID = UUID("00000000-0000-0000-0000-000000000000")
    EXPORT_FAILED_MESSAGE = (
        "Export failed. Please try again. If the issue persists, contact support."
    )

    # extension -> (expected mime type, JDSourceFormat)
    _ALLOWED_UPLOAD_TYPES = {
        "pdf": ("application/pdf", JDSourceFormat.PDF),
        "docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            JDSourceFormat.DOCX,
        ),
    }

    def __init__(
        self,
        repository: JDRepository,
        hash_service: HashService,
        audit_service: AuditService,
        storage_service: StorageService,
    ):
        self.repository = repository
        self.hash_service = hash_service
        self.audit_service = audit_service
        self.storage_service = storage_service

    def persist_processed_jd(
        self,
        *,
        title: str,
        raw_text: str,
        jurisdiction: str,
        min_experience_years: float | None,
        education_criteria: dict | None,
        source_format: JDSourceFormat,
        file_path: str | None,
        created_by: str,
        content_hash: str,
        extraction: JDExtractionResponse,
        skill_repository: SkillRepository,
        skill_matches: list[SkillMatchResult],
        embedding: list[float],
        embedding_model_version_id: UUID,
        input_text_hash: str,
        existing_jd_id: UUID | None = None,
        version_number: int = 1,
        parent_jd_id: UUID | None = None,
        lineage_root_id: UUID | None = None,
    ) -> UUID | None:
        """
        The Persistence stage of the async JD processing pipeline: writes
        JobDescription + JDSkill + UnknownSkill + JDEmbedding + audit log in
        one transaction. Returns the new jd_id, or None if a duplicate was
        detected right before insert (final safety net against the race
        widened by asynchronous processing).

        existing_jd_id absent (default) means this is a normal create run.
        When present, this is an update-triggered reprocess run: the given
        JD is deactivated and the new row is inserted as the next version
        of its lineage, instead of a brand-new standalone JD.
        """
        is_reprocess = existing_jd_id is not None
        try:
            duplicate = (
                self.repository.get_duplicate_excluding_lineage(
                    content_hash=content_hash, lineage_root_id=lineage_root_id,
                )
                if is_reprocess
                else self.repository.get_by_content_hash(content_hash)
            )
            if duplicate:
                return None

            if is_reprocess:
                existing_jd = self.repository.get_by_id(jd_id=existing_jd_id)
                self.repository.deactivate_version(existing_jd)

            # A JD only ever reaches persistence after every prior stage
            # (extraction, JSON validation, skill normalization, embedding)
            # has already succeeded - so by this point the pipeline itself
            # is done. Whether it's fully or only partially verified comes
            # down to skill_matches: any raw skill that didn't resolve to a
            # canonical_skill_id has a jd_unknown_skills row below, so this
            # JD can only be VERIFIED once none of them do.
            is_verified = (
                JDVerificationStatus.VERIFIED
                if all(match.canonical_skill_id for match in skill_matches)
                else JDVerificationStatus.PARTIALLY_VERIFIED
            )

            job_description = JobDescription(
                title=title,
                raw_text=raw_text,
                jurisdiction=jurisdiction,
                min_experience_years=min_experience_years,
                education_criteria=education_criteria,
                source_format=source_format,
                file_path=file_path,
                content_hash=content_hash,
                version_number=version_number,
                is_active_version=True,
                parent_jd_id=parent_jd_id,
                lineage_root_id=lineage_root_id,
                created_by=created_by,
                # extracted_json: the full AI-parsed JD JSON, as extracted
                # (pre-normalization) — required_skills: just the two skill
                # lists, kept separately for quick access without parsing
                # the larger blob.
                extracted_json=extraction.model_dump(mode="json"),
                required_skills={
                    "required": extraction.required_skills,
                    "preferred": extraction.preferred_skills,
                },
                is_verified=is_verified,
            )
            job_description = self.repository.create_job_description(job_description)

            for match in skill_matches:
                if match.canonical_skill_id:
                    skill_repository.create_jd_skill(
                        jd_id=job_description.id,
                        canonical_skill_id=match.canonical_skill_id,
                        mandatory=match.mandatory,
                        match_tier=match.match_tier.value,
                        verification_status=verification_status_for_tier(match.match_tier),
                        confidence=match.confidence,
                        # weight is reserved for future business-set scoring
                        # input, not populated by the automated pipeline.
                    )
                    skill_repository.bump_occurrence_count(match.canonical_skill_id)
                else:
                    unknown_skill = skill_repository.upsert_unknown_skill(
                        match.raw_text, normalized_key=match.normalized_text
                    )
                    skill_repository.link_unknown_skill_to_jd(
                        job_description.id, unknown_skill.id, mandatory=match.mandatory
                    )

            self.repository.create_jd_embedding(
                jd_id=job_description.id,
                embedding=embedding,
                embedding_model_version_id=embedding_model_version_id,
                input_text_hash=input_text_hash,
            )

            self.audit_service.log(
                actor_id=created_by,
                actor_role="HR_ADMIN",
                action_type=ActionType.JD_REPROCESSED if is_reprocess else ActionType.JD_CREATED,
                entity_type=EntityType.JOB_DESCRIPTION,
                entity_id=job_description.id,
                jurisdiction=job_description.jurisdiction,
                details={
                    "title": job_description.title,
                    "version_number": job_description.version_number,
                    "source_format": job_description.source_format.value,
                    **({"previous_jd_id": str(existing_jd_id)} if is_reprocess else {}),
                },
            )

            self.repository.commit()
            return job_description.id

        except Exception:
            self.repository.rollback()
            raise

    def validate_and_store_file(self, file: UploadFile, org_id: UUID | None) -> str:
        """
        Validation + Storage stages for the async JD processing pipeline:
        validates the upload and stores it in Supabase, synchronously, in
        the request. Text extraction happens later, in the pipeline's own
        Text Extraction stage, so a slow parse never blocks the response.

        Returns the storage object path.
        """
        extension = self.validate_upload_type(file)
        file_content = file.file.read()

        object_path = f"org_{org_id}/jd/{uuid4()}.{extension}"
        return self.storage_service.upload_file(
            bucket_name=self.JD_STORAGE_BUCKET,
            file_path=object_path,
            file_content=file_content,
            content_type=file.content_type,
        )

    def validate_upload_type(self, file: UploadFile) -> str:
        extension = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
        allowed = self._ALLOWED_UPLOAD_TYPES.get(extension)

        if allowed is None:
            raise BadRequestError("Only PDF and DOCX files are supported for upload.")

        expected_mime_type, _ = allowed
        if file.content_type and file.content_type != expected_mime_type:
            raise BadRequestError("Uploaded file's content type does not match its extension.")

        return extension

    @staticmethod
    def _resolve_source_format(file_path: str | None) -> JDSourceFormat:
        if not file_path:
            return JDSourceFormat.TEXT
        extension = file_path.rsplit(".", 1)[-1].lower()
        return JDSourceFormat.PDF if extension == "pdf" else JDSourceFormat.DOCX

    def _build_job_description(self,
                               request: CreateJDRequest,
                               *,
                               create_by: str,
                               version_number: int,
                               parent_jd_id: UUID| None,
                               lineage_root_id: UUID | None,
                               source_format: JDSourceFormat = JDSourceFormat.TEXT,
                               file_path: str | None = None,
                               fallback_raw_text: str | None = None) -> JobDescription:
        # A metadata-only update omits raw_text entirely (raw_text is
        # optional on UpdateJDRequest) — raw_text is NOT NULL on the model,
        # so the previous version's text carries forward unchanged.
        raw_text = request.raw_text or fallback_raw_text
        return JobDescription(
            title= request.title,
            raw_text= raw_text,
            jurisdiction= request.jurisdiction,
            min_experience_years= request.min_experience_years,
            education_criteria= (
                request.education_criteria.model_dump()
                if request.education_criteria
                else None
            ),
            source_format= source_format,
            file_path= file_path,
            content_hash= self.hash_service.generate_hash(raw_text),
            version_number= version_number,
            is_active_version= True,
            parent_jd_id= parent_jd_id,
            lineage_root_id= lineage_root_id,
            created_by= create_by
        )



    def get_by_id(self, jd_id: str) -> JobDescription | None:
        job_description = self.repository.get_by_id(jd_id=jd_id)

        if not job_description:
            raise HTTPException(
                status_code=404,
                detail=f"Job Description with ID {jd_id} not found."
            )

        return GetJDResponse(
            created_at=job_description.created_at,
            created_by=job_description.created_by,
            id=job_description.id,
            job_id=job_description.job_id,
            is_active_version=job_description.is_active_version,
            jurisdiction=job_description.jurisdiction,
            min_experience_years=job_description.min_experience_years,
            notice_period=job_description.notice_period,
            raw_text=job_description.raw_text,
            required_skills=job_description.required_skills,
            source_format=job_description.source_format.value,
            title=job_description.title,
            updated_at=job_description.updated_at,
            version_number=job_description.version_number,
            education_criteria=job_description.education_criteria,
            extracted_json=job_description.extracted_json,
            is_verified=job_description.is_verified.value,
        )

    def get_all_jds(self, is_active_version: bool) -> list[JobDescription]:
        return self.repository.get_all_jds(is_active_version=is_active_version)

    def download_jd_file(self, jd_id: UUID) -> tuple[bytes, str, str]:
        """
        Returns (file_bytes, filename, content_type) for a JD:
          - TEXT-sourced JDs (no uploaded document) get raw_text rendered
            into a DOCX on the fly.
          - PDF/DOCX-sourced JDs return the original uploaded file, fetched
            from Supabase Storage via file_path.
        """
        existing_jd = self.repository.get_by_id(jd_id=jd_id)

        if not existing_jd:
            raise NotFoundError(f"Job Description with ID {jd_id} not found.")

        safe_title = re.sub(r"[^A-Za-z0-9 _-]", "", existing_jd.title).strip().replace(" ", "_") or "job_description"

        if existing_jd.source_format == JDSourceFormat.TEXT:
            file_bytes = self._render_docx(existing_jd.raw_text)
            content_type = self._ALLOWED_UPLOAD_TYPES["docx"][0]
            return file_bytes, f"{safe_title}.docx", content_type

        if not existing_jd.file_path:
            raise NotFoundError(f"No stored document found for Job Description with ID {jd_id}.")

        file_bytes = self.storage_service.download_file(
            bucket_name=self.JD_STORAGE_BUCKET,
            file_path=existing_jd.file_path,
        )
        extension = existing_jd.file_path.rsplit(".", 1)[-1].lower()
        content_type = self._ALLOWED_UPLOAD_TYPES.get(extension, (None,))[0] or "application/octet-stream"

        return file_bytes, f"{safe_title}.{extension}", content_type

    @staticmethod
    def _render_docx(raw_text: str) -> bytes:
        document = Document()
        for line in raw_text.splitlines() or [raw_text]:
            document.add_paragraph(line)
        buffer = io.BytesIO()
        document.save(buffer)
        return buffer.getvalue()



    def update_jd(
        self,
        jd_id: UUID,
        request: UpdateJDRequest,
        updated_by: str,
        file_path: str | None = None,
    ) -> UpdateJDResponse | JDReprocessRequired:
        """
        Metadata-only changes (title/jurisdiction/min_experience_years/
        education_criteria, with raw_text and the document unchanged) are
        applied synchronously here, same as before. If raw_text actually
        changed or a new file was uploaded, this returns JDReprocessRequired
        instead of performing the update itself — those are the only two
        triggers the finalized design re-runs Extraction/Normalization/
        Matching/Embedding for, and that pipeline is too slow (Gemini +
        embedding model) to run inline, exactly why JD creation already
        runs it via Celery rather than in the request.
        """

        existing_jd = self.repository.get_by_id(jd_id=jd_id)

        if not existing_jd:
            raise NotFoundError(f"Job Description with ID {jd_id} not found.")

        if not existing_jd.is_active_version:
            raise BadRequestError(f"Cannot update an inactive version of the Job Description with ID {jd_id}.")

        if self.repository.has_active_campaign(jd_id):
            raise ConflictError(
                f"Cannot update Job Description with ID {jd_id}: it has an active hiring campaign assigned."
            )

        if existing_jd.lineage_root_id:
            lineage_root_id = existing_jd.lineage_root_id
        else:
            lineage_root_id = existing_jd.id

        # Duplicate check against every OTHER JD lineage - a match within
        # this same lineage (e.g. resubmitting unchanged text) is not a
        # duplicate, it's just this JD being updated.
        if request.raw_text:
            content_hash = self.hash_service.generate_hash(request.raw_text)
            duplicate_jd = self.repository.get_duplicate_excluding_lineage(
                content_hash=content_hash,
                lineage_root_id=lineage_root_id,
            )
            if duplicate_jd:
                raise DuplicateJDException(
                    DuplicateJDInfo(
                        message="Duplicate job description found.",
                        existing_jd=ExistingJDInfo(
                            id=duplicate_jd.id,
                            title=duplicate_jd.title,
                            version_number=duplicate_jd.version_number,
                            created_at=duplicate_jd.created_at,
                        ),
                        actions=["View Existing", "Create New Version"],
                    )
                )

        raw_text_changed = bool(request.raw_text) and request.raw_text != existing_jd.raw_text
        file_replaced = file_path is not None

        if raw_text_changed or file_replaced:
            education_criteria = (
                request.education_criteria.model_dump() if request.education_criteria else None
            )
            return JDReprocessRequired(
                existing_jd_id=existing_jd.id,
                version_number=existing_jd.version_number + 1,
                parent_jd_id=existing_jd.id,
                lineage_root_id=lineage_root_id,
                title=request.title,
                jurisdiction=request.jurisdiction,
                min_experience_years=request.min_experience_years,
                education_criteria=education_criteria,
                # Only one of these is set: raw_text for the JSON-body path,
                # file_path for the file-upload path — the pipeline's own
                # TEXT_EXTRACTION stage handles the file case, same as create.
                raw_text=request.raw_text if raw_text_changed else None,
                file_path=file_path,
                old_file_path=existing_jd.file_path,
                updated_by=updated_by,
            )

        # Metadata-only path — unchanged synchronous behavior, document and
        # raw_text both carried forward from the current active version.
        self.repository.deactivate_version(existing_jd)

        new_jd = self._build_job_description(
            request= request,
            create_by= updated_by,
            version_number= existing_jd.version_number + 1,
            parent_jd_id= existing_jd.id,
            lineage_root_id= lineage_root_id,
            source_format= existing_jd.source_format,
            file_path= existing_jd.file_path,
            fallback_raw_text= existing_jd.raw_text,
        )

        new_jd = self.repository.create_job_description(new_jd)
        self.audit_service.log(
            actor_id=updated_by,
            actor_role="HR_ADMIN",
            action_type= ActionType.JD_VERSION_CREATED,
            entity_type= EntityType.JOB_DESCRIPTION,
            entity_id=new_jd.id,
            jurisdiction=new_jd.jurisdiction,
            details={
                "title": new_jd.title,
                "version_number": new_jd.version_number,
                "source_format": new_jd.source_format.value,
            }
        )

        self.repository.commit()

        return UpdateJDResponse(
            id= new_jd.id,
            title= new_jd.title,
            version_number= new_jd.version_number,
            updated_by= updated_by,
        )

    def deactivate_jd(self, jd_id: UUID, updated_by:str) -> UpdateJDResponse:
        existing_jd = self.repository.get_by_id(jd_id=jd_id)

        if not existing_jd:
            raise NotFoundError(f"Job Description with ID {jd_id} not found.")

        if not existing_jd.is_active_version:
            raise BadRequestError(f"Job Description with ID {jd_id} is already inactive.")

        if self.repository.has_active_campaign(jd_id):
            raise ConflictError(
                f"Cannot delete Job Description with ID {jd_id}: it has an active hiring campaign assigned."
            )

        self.repository.deactivate_version(existing_jd)
        # Soft delete: the row stays for history/audit, closed_at marks it
        # as no longer usable (checked by CampaignService.create_campaign).
        existing_jd.closed_at = datetime.now(timezone.utc)

        self.audit_service.log(
            actor_id=updated_by,
            actor_role="HR_ADMIN",
            action_type= ActionType.JD_CLOSED,
            entity_type= EntityType.JOB_DESCRIPTION,
            entity_id=existing_jd.id,
            jurisdiction=existing_jd.jurisdiction,
            details={
                "title": existing_jd.title,
                "version_number": existing_jd.version_number,
                "source_format": existing_jd.source_format.value,
            }
        )

        self.repository.commit()

        return UpdateJDResponse(
            id= existing_jd.id,
            title= existing_jd.title,
            version_number= existing_jd.version_number,
            updated_by= updated_by,
        )

    def search_job_descriptions(
        self,
        request: JDSearchRequest,
    )-> PaginatedJDResponse:
        records, total = self.repository.search(request=request)

        items = [JDMapper.to_list_item(jd) for jd in records]

        return PaginatedJDResponse(
            total=total,
            page=request.page,
            size=request.size,
            items=items
        )
    
    # def export_jd_list(
    #     self,
    #     request: JDSearchRequest,
    # ):
    #     records = self.repository.export_jd_list(request)

    #     excel_file = ExcelExport.export_jd_list(records)

    #     filename = (
    #         f"JD_List_Export_"
    #         f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    #     )

    #     return StreamingResponse(
    #         excel_file,
    #         media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    #         headers={
    #             "Content-Disposition": f'attachment; filename="{filename}"'
    #         },
    #     )

    def export_jd_list(
        self,
        request: JDSearchRequest,
        exported_by: str,
        actor_role: str | None,
    ):
        total_records = self.repository.count_export_jd_list(request)
        details = self._build_export_audit_details(
            request=request,
            total_exported_records=0,
        )

        if total_records > self.MAX_JD_EXPORT_RECORDS:
            details["status"] = "FAILED"
            details["failure_reason"] = "EXPORT_LIMIT_EXCEEDED"
            details["matched_records"] = total_records
            self._log_jd_export(
                actor_id=exported_by,
                actor_role=actor_role,
                entity_id=self.EXPORT_AUDIT_ENTITY_ID,
                jurisdiction=request.jurisdiction,
                details=details,
            )
            self.repository.commit()
            raise BadRequestError(
                f"Export limit exceeded. Apply filters before exporting. "
                f"Maximum allowed records: {self.MAX_JD_EXPORT_RECORDS}."
            )

        try:
            records = self.repository.export_jd_list(request)

            user_names = {}

            for jd in records:

                # Created By Full Name
                if jd.created_by not in user_names:
                    user_names[jd.created_by] = (
                        self.repository.get_user_full_name(
                            jd.created_by
                        )
                    )

                # Flatten Education Criteria
                education = jd.education_criteria or {}

                degree = education.get("degree", "")
                field = education.get("field", "")

                jd.education_display = " - ".join(
                    filter(None, [degree, field])
                )

                # Linked Campaign Count
                jd.linked_campaign_count = (
                    self.repository.get_linked_campaign_count(
                        jd.id
                    )
                )

            excel_file = ExcelExport.export_jd_list(
                records,
                user_names,
            )
        except Exception:
            logger.exception("Failed to generate JD list export.")
            details["status"] = "FAILED"
            details["matched_records"] = total_records
            self._log_jd_export(
                actor_id=exported_by,
                actor_role=actor_role,
                entity_id=self.EXPORT_AUDIT_ENTITY_ID,
                jurisdiction=request.jurisdiction,
                details=details,
            )
            self.repository.commit()
            raise HTTPException(
                status_code=500,
                detail=self.EXPORT_FAILED_MESSAGE,
            )

        details = self._build_export_audit_details(
            request=request,
            total_exported_records=len(records),
        )
        details["status"] = "SUCCESS"
        self._log_jd_export(
            actor_id=exported_by,
            actor_role=actor_role,
            entity_id=self.EXPORT_AUDIT_ENTITY_ID,
            jurisdiction=request.jurisdiction,
            details=details,
        )
        self.repository.commit()

        filename = (
            f"JD_List_Export_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition":
                f'attachment; filename="{filename}"'
            },
        )
    def export_single_jd(
        self,
        jd_id: UUID,
        exported_by: str,
        actor_role: str | None,
    ):
        # Get JD
        jd = self.repository.export_single_jd(jd_id)

        if not jd:
            self._log_jd_export(
                actor_id=exported_by,
                actor_role=actor_role,
                entity_id=self.EXPORT_AUDIT_ENTITY_ID,
                jurisdiction=None,
                details={
                    "filters": {
                        "jd_id": str(jd_id),
                    },
                    "total_exported_records": 0,
                    "status": "FAILED",
                    "failure_reason": "JD_NOT_FOUND",
                },
            )
            self.repository.commit()
            raise NotFoundError(
                f"Job Description with ID {jd_id} not found."
            )

        # Determine lineage root
        lineage_root_id = (
            jd.lineage_root_id
            if jd.lineage_root_id
            else jd.id
        )

        # Get version history
        version_history = self.repository.get_version_history(
            lineage_root_id
        )
        created_by_name = self.repository.get_user_full_name(
            jd.created_by
        )

        linked_campaigns = self.repository.get_linked_campaigns(
            jd.id
        )

        try:
            # Generate Excel
            excel_file = ExcelExport.export_single_jd(
                jd,
                version_history,
                created_by_name=created_by_name,
                linked_campaigns=linked_campaigns,
            )
        except Exception:
            logger.exception("Failed to generate single JD export for JD %s.", jd_id)
            self._log_jd_export(
                actor_id=exported_by,
                actor_role=actor_role,
                entity_id=jd.id,
                jurisdiction=jd.jurisdiction,
                details={
                    "filters": {
                        "jd_id": str(jd_id),
                    },
                    "total_exported_records": 0,
                    "status": "FAILED",
                },
            )
            self.repository.commit()
            raise HTTPException(
                status_code=500,
                detail=self.EXPORT_FAILED_MESSAGE,
            )

        self._log_jd_export(
            actor_id=exported_by,
            actor_role=actor_role,
            entity_id=jd.id,
            jurisdiction=jd.jurisdiction,
            details={
                "filters": {
                    "jd_id": str(jd_id),
                },
                "total_exported_records": 1,
                "status": "SUCCESS",
            },
        )
        self.repository.commit()

        filename = (
            f"{jd.title.replace(' ', '_')}_"
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        )

        return StreamingResponse(
            excel_file,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition":
                f'attachment; filename="{filename}"'
            },
        )

    def _build_export_audit_details(
        self,
        request: JDSearchRequest,
        total_exported_records: int,
    ) -> dict:
        return {
            "filters": {
                "search": request.search,
                "jurisdiction": request.jurisdiction,
                "active": request.active,
                "source_format": request.source_format,
                "sort_by": request.sort_by,
                "order": request.order,
            },
            "total_exported_records": total_exported_records,
        }

    def _log_jd_export(
        self,
        *,
        actor_id: str,
        actor_role: str | None,
        entity_id: UUID,
        jurisdiction: str | None,
        details: dict,
    ) -> None:
        self.audit_service.log(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=ActionType.JD_EXPORTED,
            entity_type=EntityType.JOB_DESCRIPTION,
            entity_id=entity_id,
            jurisdiction=jurisdiction,
            details=details,
        )
