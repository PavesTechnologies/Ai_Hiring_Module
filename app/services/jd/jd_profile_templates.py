"""
JD profile templates (Phase 3 Step 7): Fresher / Experienced / Domain.

- Suggested in calibration from the JD (HR confirms it, like role domain):
  Fresher when the minimum is at most 1 year or the title says junior /
  graduate / fresher / trainee / intern / entry level; Domain when the role
  domain is IT operations or the JD lists certifications; else Experienced.
- Defaults: a setting the JD leaves unset takes the template's value
  (jd_calibration_rules); choosing a template prefills the calibration form.
- Evidence sections: an exact scan match in one of them auto-confirms a core
  skill (v2); paraphrase proposes lines from the narrative ones among them
  (experience, projects). No template = the sections used before templates.
"""
import re
from dataclasses import dataclass

from app.services.jd.jd_calibration_rules import FRESHER_MAX_MIN_YEARS, jd_profile
from app.services.skills.resume_sections import (
    SECTION_EXPERIENCE, SECTION_PROJECTS, SECTION_SKILLS, SECTION_SUMMARY, STRONG_SECTIONS,
)

PROFILE_FRESHER = "FRESHER"
PROFILE_EXPERIENCED = "EXPERIENCED"
PROFILE_DOMAIN = "DOMAIN"
PROFILE_TEMPLATE_KEYS = (PROFILE_FRESHER, PROFILE_EXPERIENCED, PROFILE_DOMAIN)

# What a template can count as evidence; experience always counts.
EVIDENCE_SECTION_OPTIONS = (SECTION_SUMMARY, SECTION_EXPERIENCE, SECTION_SKILLS, SECTION_PROJECTS)
REQUIRED_EVIDENCE_SECTIONS = frozenset({SECTION_EXPERIENCE})
# Sections written in sentences - the ones paraphrase searches.
NARRATIVE_SECTIONS = frozenset({SECTION_EXPERIENCE, SECTION_PROJECTS})

_FRESHER_TITLE = re.compile(r"(?<![a-z])(junior|jr\.?|graduate|freshers?|trainee|interns?|internship|entry[- ]level)"
                            r"(?![a-z])")


@dataclass(frozen=True)
class ProfileTemplateSuggestion:
    key: str
    basis: str        # "experience" / "title" / "role domain" / "certifications" / "default"


def suggest_profile_template(*, title: str | None, min_years: float | None, role_domain: str | None,
                             certification_count: int) -> ProfileTemplateSuggestion:
    if min_years is not None and min_years <= FRESHER_MAX_MIN_YEARS:
        return ProfileTemplateSuggestion(PROFILE_FRESHER, "experience")
    if _FRESHER_TITLE.search((title or "").lower()):
        return ProfileTemplateSuggestion(PROFILE_FRESHER, "title")
    if role_domain == "IT_OPERATIONS":
        return ProfileTemplateSuggestion(PROFILE_DOMAIN, "role domain")
    if certification_count:
        return ProfileTemplateSuggestion(PROFILE_DOMAIN, "certifications")
    return ProfileTemplateSuggestion(PROFILE_EXPERIENCED, "default")


def jd_evidence_sections(job_description) -> frozenset:
    """The profile template's evidence sections, else the strong sections (summary, experience, skills)."""
    sections = getattr(jd_profile(job_description), "evidence_sections", None) or []
    valid = frozenset(s for s in sections if s in EVIDENCE_SECTION_OPTIONS)
    return valid if valid else STRONG_SECTIONS


def jd_auto_confirm_sections(job_description) -> frozenset:
    """
    Where an exact scan may auto-confirm (on a line where the candidate does
    the work): the narrative sections among the template's evidence
    sections - Experience, plus Projects on a Fresher template - else
    Experience alone. Skills lists and summaries always go to the vote.
    """
    if jd_profile(job_description) is None:
        return frozenset({SECTION_EXPERIENCE})
    return (jd_evidence_sections(job_description) & NARRATIVE_SECTIONS) or frozenset({SECTION_EXPERIENCE})


def jd_paraphrase_sections(job_description, configured: list | None) -> list[str]:
    """The narrative evidence sections of the JD's template, else the configured ones."""
    if jd_profile(job_description) is None:
        return list(configured or [SECTION_EXPERIENCE])
    return sorted(jd_evidence_sections(job_description) & NARRATIVE_SECTIONS)
