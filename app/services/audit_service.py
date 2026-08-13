import csv
import io
import json
from datetime import datetime
from uuid import UUID

from app.enums.constants import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from app.exceptions.campaign_exceptions import CampaignException
from app.models.compliance import AuditLog
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.schemas.audit.audit_log_schema import AuditLogEntryResponse, AuditLogSearchResponse

_CSV_HEADERS = [
    "id", "actor_id", "actor_name", "actor_role", "action_type",
    "entity_type", "entity_id", "campaign_id", "detail", "created_at",
]


class AuditService:
    def __init__(self, repository: AuditRepository, campaign_repo: CampaignRepository | None = None):
        self.repository = repository
        # Epic 3 Fix 5: optional, additive - every pre-existing call site
        # (get_campaign_service et al.) is unaffected; only search_audit_log/
        # export_audit_log_csv need it, for the same batched actor-name
        # resolution CampaignService.get_campaign_timeline already uses
        # (CampaignRepository.get_user_names) - not duplicated here.
        self.campaign_repo = campaign_repo


    def log(
        self,
        *,
        actor_id: str,
        actor_role: str | None,
        action_type: str,
        entity_type: str,
        entity_id: UUID,
        details: dict | None = None,
        campaign_id: UUID | None = None,
        jurisdiction: str | None = None,
        ip_address: str | None = None,
        session_id: UUID | None = None,request_id: UUID | None = None,
        ) -> AuditLog:
        audit = AuditLog(
            actor_id=actor_id,
            actor_role=actor_role,
            action_type=action_type,
            entity_type=entity_type,
            entity_id=entity_id,
            campaign_id=campaign_id,
            jurisdiction=jurisdiction,
            detail=details,
            ip_address=ip_address,
            session_id=session_id,
            request_id=request_id,
            )
        return self.repository.create(audit)

    def get_campaign_scoring_history(
        self,
        campaign_id: UUID,
    ):
        return self.repository.get_campaign_scoring_history(
            campaign_id
        )

    def get_latest_entry(self, campaign_id: UUID, action_type: str):
        return self.repository.get_latest_entry(campaign_id, action_type)

    # ------------------------------------------------------------------
    # Epic 3 Fix 5: global, cross-entity audit log search + CSV export.
    # audit_log-only, deliberately not merging in campaign_candidate_
    # stage_history/bulk_upload_job like CampaignService.get_campaign_timeline
    # does - that merge is a single-campaign activity feed with its own
    # dedicated view already; rebuilding it here would create two subtly
    # different sources of truth for the same events. HR_ADMIN-gated at
    # the route layer, matching every other audit-adjacent endpoint.
    # ------------------------------------------------------------------

    def _validate_search_params(
        self, page: int, page_size: int, created_from: datetime | None, created_to: datetime | None,
    ) -> None:
        if page < 1:
            raise CampaignException("page must be >= 1.", 422)
        if page_size < 1 or page_size > MAX_PAGE_SIZE:
            raise CampaignException(f"page_size must be between 1 and {MAX_PAGE_SIZE}.", 422)
        if created_from is not None and created_to is not None and created_from > created_to:
            raise CampaignException("created_from must not be after created_to.", 422)

    def _resolve_actor_names(self, actor_ids: set) -> dict:
        if not actor_ids or self.campaign_repo is None:
            return {}
        return self.campaign_repo.get_user_names(list(actor_ids))

    @staticmethod
    def _enum_value(value) -> str | None:
        """Route/query-param layer passes ActionType/EntityType enum members - the
        repository (matching get_latest_entry/get_campaign_scoring_history's
        existing str-typed convention) filters on the raw string value."""
        if value is None:
            return None
        return value.value if hasattr(value, "value") else value

    def search_audit_log(
        self,
        *,
        entity_type=None,
        entity_id: UUID | None = None,
        actor_id: str | None = None,
        campaign_id: UUID | None = None,
        action_type=None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> AuditLogSearchResponse:
        self._validate_search_params(page, page_size, created_from, created_to)

        rows, total = self.repository.search(
            entity_type=self._enum_value(entity_type),
            entity_id=entity_id,
            actor_id=actor_id,
            campaign_id=campaign_id,
            action_type=self._enum_value(action_type),
            created_from=created_from,
            created_to=created_to,
            page=page,
            page_size=page_size,
        )

        actor_names = self._resolve_actor_names({row.actor_id for row in rows if row.actor_id})

        def resolve(actor_id_value) -> str:
            if not actor_id_value:
                return "System"
            return actor_names.get(str(actor_id_value), "System")

        items = [
            AuditLogEntryResponse(
                id=row.id,
                actor_id=row.actor_id,
                actor_name=resolve(row.actor_id),
                actor_role=row.actor_role,
                action_type=row.action_type.value,
                entity_type=row.entity_type.value,
                entity_id=row.entity_id,
                campaign_id=row.campaign_id,
                detail=row.detail,
                created_at=row.created_at,
            )
            for row in rows
        ]

        return AuditLogSearchResponse(items=items, page=page, page_size=page_size, total=total)

    def export_audit_log_csv(
        self,
        *,
        entity_type=None,
        entity_id: UUID | None = None,
        actor_id: str | None = None,
        campaign_id: UUID | None = None,
        action_type=None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ):
        """
        No row cap: iter_for_export() yields one row at a time via keyset
        pagination (never materializes the full result set), and this
        generator writes/yields one CSV line at a time - a StreamingResponse
        built from this never holds more than a handful of rows in memory
        regardless of how large the filtered result set is.

        detail is dumped as a raw JSON string in its own column, not
        flattened - its shape varies enormously by action_type (confirmed
        during the Epic 3 investigation), so a per-row JSON string is the
        only representation that doesn't lose or misalign data across rows
        with different action_types.
        """
        entity_type_value = self._enum_value(entity_type)
        action_type_value = self._enum_value(action_type)
        actor_name_cache: dict = {}

        def resolve(actor_id_value) -> str:
            if not actor_id_value:
                return "System"
            key = str(actor_id_value)
            if key not in actor_name_cache:
                names = self._resolve_actor_names({actor_id_value})
                actor_name_cache[key] = names.get(key, "System")
            return actor_name_cache[key]

        def _generate():
            buffer = io.StringIO()
            writer = csv.writer(buffer)

            writer.writerow(_CSV_HEADERS)
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate(0)

            for row in self.repository.iter_for_export(
                entity_type=entity_type_value,
                entity_id=entity_id,
                actor_id=actor_id,
                campaign_id=campaign_id,
                action_type=action_type_value,
                created_from=created_from,
                created_to=created_to,
            ):
                writer.writerow([
                    str(row.id),
                    row.actor_id or "",
                    resolve(row.actor_id),
                    row.actor_role or "",
                    row.action_type.value,
                    row.entity_type.value,
                    str(row.entity_id),
                    str(row.campaign_id) if row.campaign_id else "",
                    json.dumps(row.detail) if row.detail is not None else "",
                    row.created_at.isoformat(),
                ])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)

        return _generate()
