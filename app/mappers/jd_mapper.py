from app.models.jd.job_descriptions import JobDescription
from app.schemas.jd.response import JDListItem



class JDMapper:

    @staticmethod
    def to_list_item(jd: JobDescription) -> JDListItem:
        return JDListItem(
            id=jd.id,
            title=jd.title,
            version_number=jd.version_number,
            jurisdiction=jd.jurisdiction,
            source_format=jd.source_format.value,
            created_by=jd.created_by,
            created_at=jd.created_at,
        )