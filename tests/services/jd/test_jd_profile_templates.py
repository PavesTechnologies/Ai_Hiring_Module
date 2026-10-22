"""JD profile templates (Phase 3 Step 7): suggestion, defaults precedence, evidence sections."""
from types import SimpleNamespace

import pytest

from app.services.jd.jd_calibration_rules import (
    default_internship_credit_pct, jd_internship_credit_pct, jd_profile, jd_tolerance_years,
)
from app.services.jd.jd_profile_templates import (
    jd_evidence_sections, jd_paraphrase_sections, suggest_profile_template,
)
from app.services.skills.resume_sections import STRONG_SECTIONS

FRESHER = SimpleNamespace(key="FRESHER", internship_credit_pct=100, experience_tolerance_years=None,
                          education_requirement="MANDATORY",
                          evidence_sections=["SUMMARY", "EXPERIENCE", "SKILLS", "PROJECTS"])
EXPERIENCED = SimpleNamespace(key="EXPERIENCED", internship_credit_pct=0, experience_tolerance_years=1.5,
                              education_requirement="PREFERRED", evidence_sections=["SUMMARY", "EXPERIENCE", "SKILLS"])


def _jd(profile=None, **kw):
    values = dict(calibration_status="CALIBRATED", calibrated_min_experience_years=kw.pop("min_years", 4),
                  extracted_json={}, min_experience_years=None, internship_credit_pct=None,
                  experience_tolerance_years=None, profile_template=getattr(profile, "key", None), profile=profile)
    values.update(kw)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("kw, expected", [
    (dict(title="Python Developer", min_years=1, role_domain="SOFTWARE", certification_count=0), ("FRESHER", "experience")),
    (dict(title="Junior Python Developer", min_years=2, role_domain="SOFTWARE", certification_count=0), ("FRESHER", "title")),
    (dict(title="Graduate Engineer Trainee", min_years=None, role_domain=None, certification_count=0), ("FRESHER", "title")),
    (dict(title="ServiceNow ITSM Consultant", min_years=4, role_domain="IT_OPERATIONS", certification_count=0),
     ("DOMAIN", "role domain")),
    (dict(title="Cloud Architect", min_years=8, role_domain="SOFTWARE", certification_count=2), ("DOMAIN", "certifications")),
    (dict(title="AI transformation lead", min_years=10, role_domain="BUSINESS", certification_count=0),
     ("EXPERIENCED", "default")),
    # "internal" / "international" are not "intern".
    (dict(title="Internal Audit Manager, International", min_years=6, role_domain=None, certification_count=0),
     ("EXPERIENCED", "default")),
])
def test_suggestion(kw, expected):
    suggestion = suggest_profile_template(**kw)
    assert (suggestion.key, suggestion.basis) == expected


def test_profile_is_read_only_when_the_row_matches_the_key():
    assert jd_profile(_jd(FRESHER)) is FRESHER
    assert jd_profile(_jd(None)) is None
    assert jd_profile(_jd(FRESHER, profile_template="DOMAIN")) is None       # stale relationship: not trusted
    assert jd_profile(SimpleNamespace()) is None                             # objects from before templates


def test_internship_the_jds_setting_then_the_template_then_the_fresher_rule():
    assert jd_internship_credit_pct(_jd(FRESHER, internship_credit_pct=50)) == 50            # set on the JD: wins
    assert jd_internship_credit_pct(_jd(FRESHER, min_years=4)) == 100                       # template default
    assert jd_internship_credit_pct(_jd(EXPERIENCED, min_years=1)) == 0                     # template beats the fresher rule
    assert default_internship_credit_pct(_jd(None, min_years=1)) == 100                     # no template: as before
    assert default_internship_credit_pct(_jd(None, min_years=4)) == 0


def test_tolerance_the_jds_setting_then_the_template_then_the_default():
    assert jd_tolerance_years(_jd(EXPERIENCED, experience_tolerance_years=2)) == 2.0
    assert jd_tolerance_years(_jd(EXPERIENCED)) == 1.5
    assert jd_tolerance_years(_jd(FRESHER)) is None                                         # template leaves the default
    assert jd_tolerance_years(_jd(None)) is None


def test_evidence_sections_come_from_the_template():
    assert jd_evidence_sections(_jd(None)) == STRONG_SECTIONS
    assert jd_evidence_sections(_jd(FRESHER)) == STRONG_SECTIONS | {"PROJECTS"}
    assert jd_evidence_sections(_jd(EXPERIENCED)) == STRONG_SECTIONS
    junk = SimpleNamespace(key="DOMAIN", evidence_sections=["NOPE"])
    assert jd_evidence_sections(_jd(junk)) == STRONG_SECTIONS                               # nothing valid: the standard ones


def test_paraphrase_searches_the_templates_narrative_sections():
    assert jd_paraphrase_sections(_jd(None), ["EXPERIENCE"]) == ["EXPERIENCE"]               # no template: the config
    assert jd_paraphrase_sections(_jd(FRESHER), ["EXPERIENCE"]) == ["EXPERIENCE", "PROJECTS"]
    assert jd_paraphrase_sections(_jd(EXPERIENCED), ["EXPERIENCE"]) == ["EXPERIENCE"]       # skills lists aren't sentences
