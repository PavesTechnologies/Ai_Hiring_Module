import io
import logging
import re
from urllib import request
from uuid import UUID, uuid4

import pypdfium2 as pdfium
from docx import Document
from fastapi import HTTPException, UploadFile

from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.models.jd.job_descriptions import JobDescription, JDSourceFormat
from app.repositories.jd_repository import JDRepository
from app.schemas.jd.request import CreateJDRequest, UpdateJDRequest, JDSearchRequest
from app.schemas.jd.response import CreateJDResponse, UpdateJDResponse, JDListItem, PaginatedJDResponse
from app.services.jd.hash_service import HashService
from app.schemas.jd.DuplicateJDInfo import DuplicateJDInfo, ExistingJDInfo
from app.services.audit_service import AuditService
from app.enums.constants import ActionType, EntityType
from app.schemas.jd.response import GetJDResponse
from app.exception_handler.exceptions import NotFoundError, BadRequestError
from app.mappers.jd_mapper import JDMapper
from app.core.storage_service import StorageService
from fastapi.responses import StreamingResponse
from datetime import datetime
from app.utils.excel_export import ExcelExport

logger = logging.getLogger(__name__)


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

    def create_jd(
        self,
        request: CreateJDRequest,
        created_by: str,
        file_path: str | None = None,
    ) -> CreateJDResponse:

        try:
            source_format = self._resolve_source_format(file_path)

            # Step 1 - Generate SHA-256 Hash
            content_hash = self.hash_service.generate_hash(
                request.raw_text
            )

            # Step 2 - Check for duplicate JD
            existing_jd = self.repository.get_by_content_hash(
                content_hash
            )

            if existing_jd:
                raise DuplicateJDException(
                    DuplicateJDInfo(
                        message="Duplicate job description found.",
                        existing_jd=ExistingJDInfo(
                            id=existing_jd.id,
                            title=existing_jd.title,
                            version_number=existing_jd.version_number,
                            created_at=existing_jd.created_at
                        ),
                        actions=["View Existing", "Create New Version"]
                    )
                )

            # Step 3 - Create Job Description
            job_description = self._build_job_description(
                request=request,
                create_by=created_by,
                version_number=1,
                parent_jd_id=None,
                lineage_root_id=None,
                source_format=source_format,
                file_path=file_path,
            )



            # Step 4 - Save
            job_description = self.repository.create_job_description(
                job_description
            )


            # Step 5 - Audit
            self.audit_service.log(
               actor_id=created_by,
               actor_role="HR_ADMIN",
               action_type= ActionType.JD_CREATED,
               entity_type= EntityType.JOB_DESCRIPTION,
               entity_id=job_description.id,
               jurisdiction=job_description.jurisdiction,
               details={
                   "title": job_description.title,
                   "version_number": job_description.version_number,
                   "source_format": job_description.source_format.value,
                   },
               )


            self.repository.commit()

            # Step 6 - Response
            return CreateJDResponse(
                id=job_description.id,
                title=job_description.title,
                version_number=job_description.version_number,
                source_format=job_description.source_format.value,
                jurisdiction=job_description.jurisdiction,
                created_by=job_description.created_by,
            )

        except Exception:
            self.repository.rollback()
            raise

    def process_uploaded_file(self, file: UploadFile, org_id: UUID | None) -> tuple[str, str]:
        """
        Validates, uploads to Supabase, and extracts raw text from an
        uploaded JD document. Called by the POST /job-descriptions/from-file
        endpoint *before* create_jd(), so create_jd() itself stays the single
        place that owns hashing, duplicate detection, saving, and audit.

        Returns (raw_text, storage_object_path).
        """
        extension = file.filename.rsplit(".", 1)[-1].lower() if file.filename and "." in file.filename else ""
        allowed = self._ALLOWED_UPLOAD_TYPES.get(extension)

        if allowed is None:
            raise BadRequestError("Only PDF and DOCX files are supported for upload.")

        expected_mime_type, _ = allowed
        if file.content_type and file.content_type != expected_mime_type:
            raise BadRequestError("Uploaded file's content type does not match its extension.")

        file_content = file.file.read()

        object_path = f"org_{org_id}/jd/{uuid4()}.{extension}"
        file_path = self.storage_service.upload_file(
            bucket_name=self.JD_STORAGE_BUCKET,
            file_path=object_path,
            file_content=file_content,
            content_type=file.content_type,
        )

        raw_text = (
            self._extract_pdf_text(file_content)
            if extension == "pdf"
            else self._extract_docx_text(file_content)
        )

        if not raw_text or not raw_text.strip():
            raise BadRequestError("Could not extract any text from the uploaded document.")

        return raw_text, file_path

    @staticmethod
    def _resolve_source_format(file_path: str | None) -> JDSourceFormat:
        if not file_path:
            return JDSourceFormat.TEXT
        extension = file_path.rsplit(".", 1)[-1].lower()
        return JDSourceFormat.PDF if extension == "pdf" else JDSourceFormat.DOCX

    @staticmethod
    def _extract_pdf_text(file_content: bytes) -> str:
        pdf = pdfium.PdfDocument(file_content)
        pages_text = [page.get_textpage().get_text_range() for page in pdf]
        return "\n".join(pages_text)

    @staticmethod
    def _extract_docx_text(file_content: bytes) -> str:
        document = Document(io.BytesIO(file_content))
        return "\n".join(paragraph.text for paragraph in document.paragraphs)

    def _build_job_description(self,
                               request: CreateJDRequest,
                               *,
                               create_by: str,
                               version_number: int,
                               parent_jd_id: UUID| None,
                               lineage_root_id: UUID | None,
                               source_format: JDSourceFormat = JDSourceFormat.TEXT,
                               file_path: str | None = None) -> JobDescription:
        return JobDescription(
            title= request.title,
            raw_text= request.raw_text,
            jurisdiction= request.jurisdiction,
            min_experience_years= request.min_experience_years,
            education_criteria= (
                request.education_criteria.model_dump()
                if request.education_criteria
                else None
            ),
            source_format= source_format,
            file_path= file_path,
            content_hash= self.hash_service.generate_hash(request.raw_text),
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
            is_active_version=job_description.is_active_version,
            jurisdiction=job_description.jurisdiction,
            min_experience_years=job_description.min_experience_years,
            raw_text=job_description.raw_text,
            required_skills=job_description.required_skills,
            source_format=job_description.source_format.value,
            title=job_description.title,
            updated_at=job_description.updated_at,
            version_number=job_description.version_number,
            education_criteria=job_description.education_criteria,
            parsed_skills=job_description.parsed_skills
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
    )-> UpdateJDResponse:

        existing_jd = self.repository.get_by_id(jd_id=jd_id)

        if not existing_jd:
            raise NotFoundError(f"Job Description with ID {jd_id} not found.")

        if not existing_jd.is_active_version:
            raise BadRequestError(f"Cannot update an inactive version of the Job Description with ID {jd_id}.")

        if existing_jd.lineage_root_id:
            lineage_root_id = existing_jd.lineage_root_id
        else:
            lineage_root_id = existing_jd.id

        # A newly uploaded file replaces the document; a metadata/text-only
        # update (file_path is None) carries forward whatever document the
        # previous active version already had.
        if file_path is not None:
            new_source_format = self._resolve_source_format(file_path)
            new_file_path = file_path
        else:
            new_source_format = existing_jd.source_format
            new_file_path = existing_jd.file_path

        self.repository.deactivate_version(existing_jd)

        new_jd = self._build_job_description(
            request= request,
            create_by= updated_by,
            version_number= existing_jd.version_number + 1,
            parent_jd_id= existing_jd.id,
            lineage_root_id= lineage_root_id,
            source_format= new_source_format,
            file_path= new_file_path,
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

        # The new document is safely stored and the version switch has
        # committed - only now is it safe to remove the superseded file, so
        # a failed update never leaves the JD without a retrievable document.
        # A cleanup failure here must not fail an already-successful update.
        if file_path is not None and existing_jd.file_path:
            try:
                self.storage_service.delete_file(
                    bucket_name=self.JD_STORAGE_BUCKET,
                    file_path=existing_jd.file_path,
                )
            except Exception:
                logger.exception(
                    "Failed to delete superseded JD document '%s' for JD %s.",
                    existing_jd.file_path,
                    existing_jd.id,
                )

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

        self.repository.deactivate_version(existing_jd)

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
