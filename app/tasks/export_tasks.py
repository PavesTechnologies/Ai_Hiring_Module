"""
M11-E05 asynchronous export generation.

S01-T03 routes any export above EXPORT_ASYNC_THRESHOLD through here instead of
generating it in the request; S03 reuses the identical task for every scheduled
run, so a recurring export and a large manual one take exactly the same code
path and can never diverge in content.
"""
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.core.celery_app import celery_app
from app.db.session import SessionLocal
from app.enums.constants import ActionType
from app.models.async_tasks import CeleryTaskLog, TaskStatus
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.candidate_note_repository import CandidateNoteRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.export_repository import ExportRepository
from app.services.audit_service import AuditService
from app.services.export.export_service import ExportService

logger = logging.getLogger(__name__)

EXPORT_TASK_TYPE = "EXPORT_GENERATE"
EXPORT_KIND = "CANDIDATE_LIST"
EXPORT_BUCKET = "exports"
DEFAULT_LINK_EXPIRY_HOURS = 24


def export_idempotency_key(campaign_id, stamp: str) -> str:
    """
    celery_task_log has no campaign_id column, so the campaign is encoded here.
    That makes the key both a genuine dedupe key and the only way to list a
    campaign's export history (S03-T03) without a schema change.
    """
    return f"{EXPORT_TASK_TYPE}:{campaign_id}:{EXPORT_KIND}:{stamp}"


def _build_service(db) -> ExportService:
    return ExportService(
        export_repo=ExportRepository(db),
        campaign_repo=CampaignRepository(db),
        audit_service=AuditService(AuditRepository(db)),
        config_repo=ConfigRepository(db),
        note_repo=CandidateNoteRepository(db),
    )


def _link_expiry_seconds(db) -> int:
    try:
        cfg = ConfigRepository(db).get_configs_by_keys(["EXPORT_LINK_EXPIRY_HOURS"])
        return int(cfg.get("EXPORT_LINK_EXPIRY_HOURS", DEFAULT_LINK_EXPIRY_HOURS)) * 3600
    except Exception:
        return DEFAULT_LINK_EXPIRY_HOURS * 3600


@celery_app.task(name="export.generate", bind=True, max_retries=2)
def generate_export_task(
    self,
    campaign_id: str,
    requested_by: str | None = None,
    options: dict | None = None,
):
    """
    Generates an export, stores it, and records the signed link on the task log.

    Delivery by email is deliberately not done here — email is a separate module
    in this project. The signed URL is written to output_summary so whatever
    dispatches mail can pick it up, and so the UI can offer the download even if
    mail never goes out.
    """
    options = options or {}
    db = SessionLocal()
    task_log = None
    started = datetime.now(timezone.utc)

    try:
        cid = UUID(str(campaign_id))
        service = _build_service(db)
        campaign = service.campaign_repo.get_by_id(cid)
        if campaign is None:
            logger.error("Export requested for unknown campaign %s", cid)
            return {"status": "SKIPPED", "reason": "campaign_not_found"}

        stamp = started.strftime("%Y%m%dT%H%M%S")
        task_log = CeleryTaskLog(
            id=uuid4(),
            task_id=str(self.request.id or uuid4()),
            idempotency_key=options.get("idempotency_key") or export_idempotency_key(cid, stamp),
            task_type=EXPORT_TASK_TYPE,
            created_by=requested_by,
            title=f"{EXPORT_KIND} export — {campaign.name}",
            status=TaskStatus.RUNNING,
            started_at=started,
        )
        db.add(task_log)
        db.commit()

        content = service.build_candidate_list_xlsx(
            cid,
            campaign_candidate_ids=[
                UUID(str(i)) for i in options["campaign_candidate_ids"]
            ] if options.get("campaign_candidate_ids") else None,
            include_rejected_sheet=bool(options.get("include_rejected_sheet")),
        )
        ext = "xlsx"
        ctype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

        object_path = f"{cid}/{EXPORT_KIND.lower()}_{stamp}.{ext}"
        signed_url = None
        try:
            from app.core.storage_service import StorageService

            storage = StorageService()
            storage.upload_file(
                bucket_name=EXPORT_BUCKET,
                file_path=object_path,
                file_content=content,
                content_type=ctype,
            )
            signed_url = storage.generate_signed_url(
                bucket_name=EXPORT_BUCKET,
                file_path=object_path,
                expires_in=_link_expiry_seconds(db),
            )
        except Exception as exc:
            # The bytes were produced successfully; storage being unavailable is
            # a delivery failure, recorded as such rather than swallowed.
            logger.exception("Export generated but could not be stored: %s", object_path)
            task_log.status = TaskStatus.FAILURE
            task_log.error_message = f"Storage upload failed: {exc}"
            task_log.completed_at = datetime.now(timezone.utc)
            db.commit()
            raise

        task_log.status = TaskStatus.SUCCESS
        task_log.completed_at = datetime.now(timezone.utc)
        task_log.duration_ms = int(
            (task_log.completed_at - started).total_seconds() * 1000
        )
        task_log.output_summary = signed_url
        db.commit()

        service.log_export(
            actor_id=requested_by,
            actor_role=options.get("actor_role"),
            campaign_id=cid,
            action_type=ActionType.CANDIDATE_LIST_EXPORTED.value,
            details={
                "title": f"{EXPORT_KIND} export generated",
                "kind": EXPORT_KIND,
                "async": True,
                "object_path": object_path,
                "bytes": len(content),
            },
        )
        return {
            "status": "SUCCESS",
            "object_path": object_path,
            "download_url": signed_url,
            "bytes": len(content),
        }

    except Exception as exc:
        logger.exception("Export task failed for campaign %s", campaign_id)
        if task_log is not None:
            try:
                task_log.status = TaskStatus.FAILURE
                task_log.error_message = str(exc)[:2000]
                task_log.completed_at = datetime.now(timezone.utc)
                db.commit()
            except Exception:
                db.rollback()
        raise
    finally:
        db.close()
