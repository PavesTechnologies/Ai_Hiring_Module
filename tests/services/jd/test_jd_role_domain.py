"""Phase 3 Step 2: the role domain suggested in JD calibration, and how it's saved and versioned."""
import pytest

from app.services.jd.jd_role_domain import suggest_role_domain


@pytest.mark.parametrize("title, core, expected", [
    ("Junior Python Developer", ["Python", "REST APIs", "git", "sql"], ("SOFTWARE", "title")),
    ("ServiceNow ITSM Consultant", ["ServiceNow ITSM", "Incident Management"], ("IT_OPERATIONS", "title")),
    ("AI transformation lead", ["AI Governance", "Program Management"], ("BUSINESS", "title")),
    ("Data Scientist", ["Python", "Statistics"], ("DATA", "title")),
    # The title says nothing: the core skills decide.
    ("Associate", ["Python", "Django", "REST APIs"], ("SOFTWARE", "skills")),
    # Nothing either way, or a tie: no suggestion (never guessed).
    ("Associate", [], (None, None)),
    ("Lead", ["Python", "Statistics"], (None, None)),
])
def test_the_suggested_role_domain(title, core, expected):
    suggestion = suggest_role_domain(title, core)
    assert (suggestion.domain, suggestion.basis) == expected


def test_keywords_match_whole_words():
    # "web" inside "cobweb" and "git" inside "digital" are not keywords.
    assert suggest_role_domain("Cobweb digital curator", []).domain is None
