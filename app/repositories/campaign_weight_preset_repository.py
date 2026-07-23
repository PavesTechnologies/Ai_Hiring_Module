from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaign_weight_preset import CampaignWeightPreset


class CampaignWeightPresetRepository:

    def __init__(
        self,
        db: Session,
    ):
        self.db = db

    def get_all_by_org(
        self,
        org_id: UUID,
    ) -> list[CampaignWeightPreset]:

        stmt = (
            select(CampaignWeightPreset)
            .where(
                CampaignWeightPreset.org_id == org_id
            )
            .order_by(
                CampaignWeightPreset.name
            )
        )

        result = self.db.execute(stmt)

        return result.scalars().all()

    def get_by_id(
        self,
        preset_id: UUID,
    ) -> CampaignWeightPreset | None:

        stmt = (
            select(CampaignWeightPreset)
            .where(
                CampaignWeightPreset.id == preset_id
            )
        )

        result = self.db.execute(stmt)

        return result.scalar_one_or_none()

    def get_by_name(
        self,
        org_id: UUID,
        name: str,
    ) -> CampaignWeightPreset | None:

        stmt = (
            select(CampaignWeightPreset)
            .where(
                CampaignWeightPreset.org_id == org_id,
                CampaignWeightPreset.name == name,
            )
        )

        result = self.db.execute(stmt)

        return result.scalar_one_or_none()

    def create(
        self,
        preset: CampaignWeightPreset,
    ) -> CampaignWeightPreset:

        self.db.add(preset)
        self.db.flush()
        self.db.refresh(preset)

        return preset

    def update(
        self,
        preset: CampaignWeightPreset,
    ) -> CampaignWeightPreset:

        self.db.flush()
        self.db.refresh(preset)

        return preset

    def delete(
        self,
        preset: CampaignWeightPreset,
    ) -> None:

        self.db.delete(preset)
        self.db.flush()

    def commit(self):

        self.db.commit()

    def rollback(self):

        self.db.rollback()