from app.models.jd.job_descriptions import JobDescription
from app.schemas.jd.response import JDListItem



class JDMapper:

    @staticmethod
    def to_list_item(
        jd: JobDescription,
        campaign_counts: dict[str, int] | None = None,
        prompt_name: str | None = None,
    ) -> JDListItem:
        campaign_counts = campaign_counts or {"active": 0, "passed": 0}
        return JDListItem(
            id=jd.id,
            job_id=jd.job_id,
            title=jd.title,
            version_number=jd.version_number,
            jurisdiction=jd.jurisdiction,
            source_format=jd.source_format.value,
            is_verified=jd.is_verified.value,
            is_active_version=jd.is_active_version,
            active_campaigns_count=campaign_counts["active"],
            passed_campaigns_count=campaign_counts["passed"],
            created_by=jd.created_by,
            created_at=jd.created_at,
            prompt_template_id=jd.prompt_template_id,
            prompt_name=prompt_name,
        )