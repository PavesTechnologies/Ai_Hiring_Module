from types import SimpleNamespace

from app.services.pii.pii_types import PIIFinding, PIIType
from app.tasks.bulk_upload_tasks import _resolve_bulk_candidate_identity


def _context(full_name, findings):
    return SimpleNamespace(
        validated_extraction=SimpleNamespace(full_name=full_name),
        pii_findings=findings,
    )


def test_resolves_full_name_email_and_phone():
    findings = [
        PIIFinding(pii_type=PIIType.EMAIL, start=0, end=1, matched_text="john@gmail.com"),
        PIIFinding(pii_type=PIIType.PHONE, start=2, end=3, matched_text="+91 9876543210"),
    ]
    full_name, email, phone = _resolve_bulk_candidate_identity(_context("John Doe", findings))

    assert full_name == "John Doe"
    assert email == "john@gmail.com"
    assert phone == "+91 9876543210"


def test_phone_is_optional():
    findings = [PIIFinding(pii_type=PIIType.EMAIL, start=0, end=1, matched_text="john@gmail.com")]
    full_name, email, phone = _resolve_bulk_candidate_identity(_context("John Doe", findings))

    assert full_name == "John Doe"
    assert email == "john@gmail.com"
    assert phone is None


def test_missing_email_resolves_to_none_for_caller_to_reject():
    full_name, email, phone = _resolve_bulk_candidate_identity(_context("John Doe", []))

    assert full_name == "John Doe"
    assert email is None
    assert phone is None


def test_missing_full_name_resolves_to_none_for_caller_to_reject():
    findings = [PIIFinding(pii_type=PIIType.EMAIL, start=0, end=1, matched_text="john@gmail.com")]
    full_name, email, phone = _resolve_bulk_candidate_identity(_context(None, findings))

    assert full_name is None
    assert email == "john@gmail.com"


def test_first_email_wins_when_resume_lists_multiple():
    findings = [
        PIIFinding(pii_type=PIIType.EMAIL, start=0, end=1, matched_text="first@gmail.com"),
        PIIFinding(pii_type=PIIType.EMAIL, start=5, end=6, matched_text="second@gmail.com"),
    ]
    _, email, _ = _resolve_bulk_candidate_identity(_context("John Doe", findings))

    assert email == "first@gmail.com"


def test_linkedin_github_portfolio_findings_do_not_affect_identity():
    findings = [
        PIIFinding(pii_type=PIIType.LINKEDIN, start=0, end=1, matched_text="linkedin.com/in/john"),
        PIIFinding(pii_type=PIIType.GITHUB, start=2, end=3, matched_text="github.com/john"),
        PIIFinding(pii_type=PIIType.PORTFOLIO, start=4, end=5, matched_text="john.dev"),
        PIIFinding(pii_type=PIIType.EMAIL, start=6, end=7, matched_text="john@gmail.com"),
    ]
    full_name, email, phone = _resolve_bulk_candidate_identity(_context("John Doe", findings))

    assert full_name == "John Doe"
    assert email == "john@gmail.com"
    assert phone is None
