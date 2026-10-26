"""
A new JD version carries the previous version's calibration forward as a draft
(status NOT_CALIBRATED) for HR to re-confirm, instead of starting empty:

  - a details edit (title, experience, education - same text): every JD skill as it
    was (tier, required, sense, requirement text) and the unknown-skill links;
  - new text or a new file (re-extracted): the tier, required flag and sense of each
    skill still on the JD; the requirement text is left for the new wording to refill,
    and skills the new text dropped are listed in calibration.

The settings (template, role domain, tolerance, internship credit, education
requirement) are carried as well; the calibrated minimum only while the JD's own
minimum is unchanged. calibration_draft_from records which version it came from.
"""
from uuid import UUID

from app.models.jd.job_descriptions import JDCalibrationStatus
from app.models.skills import JDSkill, JDUnknownSkill

CARRIED_SETTINGS = ("experience_tolerance_years", "education_requirement", "internship_credit_pct",
                    "role_domain", "profile_template")
# Copied as they are on a details edit (the JD text and its extraction are unchanged).
JD_SKILL_FIELDS = ("canonical_skill_id", "mandatory", "weight", "importance", "alias_skill_ids", "confidence",
                   "sense_key", "required", "requirement_text", "verified_by", "verified_at", "match_tier",
                   "verification_status")
UNKNOWN_LINK_FIELDS = ("unknown_skill_id", "mandatory", "status", "resolution", "resolved_canonical_skill_id",
                       "resolved_by", "resolved_at")


def has_calibration_to_carry(old_jd) -> bool:
    """Calibrated, or itself a draft carried from a calibrated version (edited again before re-confirming)."""
    return (getattr(old_jd, "calibration_status", None) == JDCalibrationStatus.CALIBRATED
            or isinstance(getattr(old_jd, "calibration_draft_from", None), UUID))


def _extracted_min(jd):
    value = ((getattr(jd, "extracted_json", None) or {}).get("experience") or {}).get("min_experience_years")
    return value if value is not None else getattr(jd, "min_experience_years", None)


def carry_settings(old_jd, new_jd) -> None:
    """The previous version's calibration settings onto the new version, as a draft."""
    if not has_calibration_to_carry(old_jd):
        return
    for field in CARRIED_SETTINGS:
        setattr(new_jd, field, getattr(old_jd, field))
    same_minimum = (_extracted_min(old_jd) == _extracted_min(new_jd)
                    and getattr(old_jd, "min_experience_years", None) == getattr(new_jd, "min_experience_years", None))
    new_jd.calibrated_min_experience_years = old_jd.calibrated_min_experience_years if same_minimum else None
    new_jd.calibration_status = JDCalibrationStatus.NOT_CALIBRATED
    new_jd.calibration_version = 0
    new_jd.calibration_draft_from = old_jd.id


def copied_jd_skills(old_rows: list, new_jd_id) -> list[JDSkill]:
    """A details edit: the JD skills exactly as they were, on the new version."""
    return [JDSkill(jd_id=new_jd_id, **{f: getattr(row, f) for f in JD_SKILL_FIELDS}) for row in old_rows]


def copied_unknown_links(old_links: list, new_jd_id) -> list[JDUnknownSkill]:
    return [JDUnknownSkill(jd_id=new_jd_id, **{f: getattr(link, f) for f in UNKNOWN_LINK_FIELDS})
            for link in old_links]


def carry_skill_calibration(old_rows: list, new_rows: list) -> None:
    """
    New text: each re-extracted skill that was on the previous version keeps its tier,
    required flag and sense. The requirement text is not carried - the calibration view
    suggests one from the new wording.
    """
    previous = {row.canonical_skill_id: row for row in old_rows}
    for row in new_rows:
        old = previous.get(row.canonical_skill_id)
        if old is None:
            continue
        row.mandatory = old.mandatory
        row.importance = old.importance
        row.sense_key = old.sense_key
        row.required = bool(getattr(old, "required", False))
