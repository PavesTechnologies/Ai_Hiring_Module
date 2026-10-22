from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.campaigns import HiringCampaign
from app.models.jd.job_descriptions import JobDescription
from app.models.jd.profile_templates import JDProfileTemplate
from app.services.campaign.score_out_of_date import LIVE_CAMPAIGN_STATUSES

_ORDER = {"FRESHER": 0, "EXPERIENCED": 1, "DOMAIN": 2}


class JDProfileTemplateRepository:
    def __init__(self, db: Session):
        self.db = db

    def list_all(self) -> list[JDProfileTemplate]:
        rows = self.db.execute(select(JDProfileTemplate)).scalars().all()
        return sorted(rows, key=lambda t: _ORDER.get(t.key, len(_ORDER)))

    def get(self, key: str) -> JDProfileTemplate | None:
        return self.db.get(JDProfileTemplate, key)

    def live_campaign_ids_using(self, key: str) -> list:
        """Live (active / paused) campaigns whose JD uses this template."""
        return list(self.db.execute(
            select(HiringCampaign.id).join(JobDescription, JobDescription.id == HiringCampaign.jd_id)
            .where(JobDescription.profile_template == key, HiringCampaign.status.in_(LIVE_CAMPAIGN_STATUSES))
        ).scalars())

    def flush(self) -> None:
        self.db.flush()

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
