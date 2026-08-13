from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models.compliance import AuditLog


class AuditRepository:

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def _build_search_filters(
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        actor_id: str | None = None,
        campaign_id: UUID | None = None,
        action_type: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
    ) -> list:
        """
        Epic 3 Fix 5: shared by search() and iter_for_export() so export is
        never a separate, potentially-drifting query path from search -
        same filter set, just consumed paginated vs. streamed. Mirrors
        CampaignCandidateRepository.get_ranked_by_campaign's filters-list
        + AND-combine convention exactly.
        """
        filters = []
        if entity_type is not None:
            filters.append(AuditLog.entity_type == entity_type)
        if entity_id is not None:
            filters.append(AuditLog.entity_id == entity_id)
        if actor_id is not None:
            filters.append(AuditLog.actor_id == actor_id)
        if campaign_id is not None:
            filters.append(AuditLog.campaign_id == campaign_id)
        if action_type is not None:
            filters.append(AuditLog.action_type == action_type)
        if created_from is not None:
            filters.append(AuditLog.created_at >= created_from)
        if created_to is not None:
            filters.append(AuditLog.created_at <= created_to)
        return filters

    def search(
        self,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        actor_id: str | None = None,
        campaign_id: UUID | None = None,
        action_type: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AuditLog], int]:
        """
        Epic 3 Fix 5: one filtered/sorted/paginated SELECT plus one
        COUNT(*) with the identical filter set, same shape as
        CampaignCandidateRepository.get_ranked_by_campaign. Sorted
        created_at DESC - idx_audit_log_campaign_id_created_at and
        idx_audit_log_created_at (Fix 3) cover the two shapes this
        produces (campaign_id-filtered vs. not); idx_audit_log_actor_id
        and idx_audit_log_entity_type_entity_id cover those filters.
        """
        filters = self._build_search_filters(
            entity_type, entity_id, actor_id, campaign_id, action_type, created_from, created_to,
        )

        total = self.db.execute(
            select(func.count()).select_from(AuditLog).where(*filters)
        ).scalar() or 0

        stmt = (
            select(AuditLog)
            .where(*filters)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        rows = list(self.db.execute(stmt).scalars().all())
        return rows, total

    def iter_for_export(
        self,
        entity_type: str | None = None,
        entity_id: UUID | None = None,
        actor_id: str | None = None,
        campaign_id: UUID | None = None,
        action_type: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        batch_size: int = 500,
    ):
        """
        Epic 3 Fix 5: no row cap on export, so this must never materialize
        the full result set - keyset ("seek") pagination on
        (created_at, id) instead of OFFSET, since OFFSET's cost grows with
        how far into the result set a batch is (irrelevant at 500 rows,
        real at an unbounded export). Same _build_search_filters() as
        search() - export is the same query, batched instead of paginated,
        never a second, potentially-drifting filter implementation.
        Yields one AuditLog at a time; the caller (AuditService.
        export_audit_log_csv) never holds more than one batch in memory.
        """
        filters = self._build_search_filters(
            entity_type, entity_id, actor_id, campaign_id, action_type, created_from, created_to,
        )

        last_created_at = None
        last_id = None
        while True:
            batch_filters = list(filters)
            if last_created_at is not None:
                batch_filters.append(
                    or_(
                        AuditLog.created_at < last_created_at,
                        and_(AuditLog.created_at == last_created_at, AuditLog.id < last_id),
                    )
                )

            stmt = (
                select(AuditLog)
                .where(*batch_filters)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(batch_size)
            )
            batch = list(self.db.execute(stmt).scalars().all())
            if not batch:
                return

            for row in batch:
                yield row

            last_created_at = batch[-1].created_at
            last_id = batch[-1].id
            if len(batch) < batch_size:
                return

    def create(
        self,
        audit_log: AuditLog,
    ) -> AuditLog:

        self.db.add(audit_log)
        self.db.flush()
        self.db.refresh(audit_log)

        return audit_log

    def get_campaign_scoring_history(
        self,
        campaign_id: UUID,
    ) -> list[AuditLog]:

        stmt = (
            select(AuditLog)
            .where(
                AuditLog.campaign_id == campaign_id,
                # CAMPAIGN_THRESHOLDS_UPDATED is kept here for backward
                # compatibility with rows written before update_scoring_configuration
                # was switched to log CAMPAIGN_SCORING_CONFIG_CHANGED like every
                # other scoring-edit path — new rows only ever use the latter.
                AuditLog.action_type.in_(
                    ["CAMPAIGN_SCORING_CONFIG_CHANGED", "CAMPAIGN_THRESHOLDS_UPDATED"]
                ),
            )
            .order_by(
                AuditLog.created_at.desc()
            )
        )

        result = self.db.execute(stmt)

        return result.scalars().all()

    def get_latest_entry(
        self,
        campaign_id: UUID,
        action_type: str,
    ) -> AuditLog | None:
        """most recent audit entry of a given type for a campaign — used to compute pause duration on resume."""
        stmt = (
            select(AuditLog)
            .where(
                AuditLog.campaign_id == campaign_id,
                AuditLog.action_type == action_type,
            )
            .order_by(AuditLog.created_at.desc())
            .limit(1)
        )
        return self.db.execute(stmt).scalars().first()

    def save(self):
        self.db.commit()