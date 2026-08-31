from datetime import datetime
from uuid import UUID

from sqlalchemy import Float, cast, func, or_, select, text
from sqlalchemy.orm import Session

from app.models.candidates import Resume
from app.models.pipeline import CampaignCandidate

# The parser emits education entries as {degree, institution} free text — there
# is no normalised degree_level (and no is_highest_degree) despite what
# Assumes. These patterns map a requested level onto the text
# actually present, including the abbreviations Indian resumes commonly use.
# Deliberately conservative: a missed match is better than a wrong one, since
# this filter silently removes candidates from a reviewer's view.
DEGREE_LEVEL_PATTERNS = {
    "PHD": ["%phd%", "%ph.d%", "%doctor%", "%d.phil%"],
    "MASTER": ["%master%", "%m.tech%", "%mtech%", "%m.sc%", "%msc%", "%mba%", "%m.e.%", "%m.a.%", "%pgdm%"],
    "BACHELOR": ["%bachelor%", "%b.tech%", "%btech%", "%b.sc%", "%bsc%", "%b.e.%", "%b.a.%", "%bca%", "%bcom%"],
    "DIPLOMA": ["%diploma%", "%polytechnic%"],
    "ASSOCIATE": ["%associate%"],
    "CERTIFICATION": ["%certificat%"],
}


class CandidateFilterRepository:
    """
    M11-E03-S02-T02/T03 — filters that live in resumes rather than on
    campaign_candidates, so they can't be expressed as plain column filters
    on the ranked-candidate query.
    """

    def __init__(self, db: Session):
        self.db = db

    def filter_candidate_ids(
        self,
        campaign_id: UUID,
        *,
        experience_min: float | None = None,
        experience_max: float | None = None,
        include_unknown_experience: bool = True,
        degree_levels: list[str] | None = None,
        uploaded_by: str | None = None,
        uploaded_from: datetime | None = None,
        uploaded_to: datetime | None = None,
        upload_type: str | None = None,   # "individual" | "bulk"
    ) -> list[UUID] | None:
        """
        Returns matching campaign_candidate ids, or None when no filter in this
        family was requested — None means "don't intersect", which is different
        from [] meaning "matched nothing".
        """
        wants_filter = any(
            v is not None
            for v in (experience_min, experience_max, uploaded_by, uploaded_from, uploaded_to, upload_type)
        ) or bool(degree_levels)
        if not wants_filter:
            return None

        stmt = (
            select(CampaignCandidate.id)
            .join(Resume, Resume.id == CampaignCandidate.resume_id)
            .where(CampaignCandidate.campaign_id == campaign_id)
        )

        # ── experience ──────────────────────────────────────────
        if experience_min is not None or experience_max is not None:
            years = cast(
                Resume.parsed_json[text("'total_experience_years'")].astext, Float
            )
            bounds = []
            if experience_min is not None:
                bounds.append(years >= experience_min)
            if experience_max is not None:
                bounds.append(years <= experience_max)
            expr = bounds[0] if len(bounds) == 1 else (bounds[0] & bounds[1])
            # A resume whose experience couldn't be parsed is unknown, not zero —
            # excluding it silently would hide real candidates.
            if include_unknown_experience:
                expr = or_(expr, years.is_(None))
            stmt = stmt.where(expr)

        # ── education level ─────────────────────────────────────
        if degree_levels:
            patterns: list[str] = []
            for level in degree_levels:
                patterns.extend(DEGREE_LEVEL_PATTERNS.get(level.upper(), []))
            if patterns:
                # any education entry whose free-text degree matches any pattern
                edu = func.jsonb_array_elements(
                    func.coalesce(Resume.parsed_json["education"], text("'[]'::jsonb"))
                ).alias("edu")
                degree_txt = func.lower(func.coalesce(text("edu->>'degree'"), text("''")))
                exists_q = (
                    select(1)
                    .select_from(edu)
                    .where(or_(*[degree_txt.like(p) for p in patterns]))
                    .exists()
                )
                stmt = stmt.where(exists_q)

        # ── upload source ───────────────────────────────────────
        if uploaded_by:
            stmt = stmt.where(Resume.uploaded_by == uploaded_by)
        if uploaded_from is not None:
            stmt = stmt.where(Resume.created_at >= uploaded_from)
        if uploaded_to is not None:
            stmt = stmt.where(Resume.created_at <= uploaded_to)
        if upload_type == "bulk":
            stmt = stmt.where(Resume.bulk_upload_job_id.isnot(None))
        elif upload_type == "individual":
            stmt = stmt.where(Resume.bulk_upload_job_id.is_(None))

        return [r[0] for r in self.db.execute(stmt).all()]

    def get_campaign_uploaders(self, campaign_id: UUID):
        """Distinct uploaders for this campaign, to populate the filter dropdown."""
        from app.models.identity import User

        return (
            self.db.query(User.id, User.full_name, func.count(CampaignCandidate.id).label("upload_count"))
            .join(Resume, Resume.uploaded_by == User.id)
            .join(CampaignCandidate, CampaignCandidate.resume_id == Resume.id)
            .filter(CampaignCandidate.campaign_id == campaign_id)
            .group_by(User.id, User.full_name)
            .order_by(func.count(CampaignCandidate.id).desc())
            .all()
        )
