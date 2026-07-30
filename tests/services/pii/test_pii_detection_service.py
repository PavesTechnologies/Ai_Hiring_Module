import pytest

from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.pii.pii_types import PIIType


@pytest.fixture
def detector():
    return PIIDetectionService()


def _of_type(findings, pii_type):
    return [f for f in findings if f.pii_type == pii_type]


class TestEmailDetection:
    def test_standard_email(self, detector):
        findings = detector.detect("Contact: john@gmail.com")
        emails = _of_type(findings, PIIType.EMAIL)
        assert len(emails) == 1
        assert emails[0].matched_text == "john@gmail.com"

    def test_mixed_case_email(self, detector):
        findings = detector.detect("Email: John.Doe@Example.COM")
        emails = _of_type(findings, PIIType.EMAIL)
        assert emails[0].matched_text == "John.Doe@Example.COM"

    def test_email_surrounded_by_punctuation(self, detector):
        findings = detector.detect("(john@gmail.com), reach out.")
        emails = _of_type(findings, PIIType.EMAIL)
        assert len(emails) == 1
        assert emails[0].matched_text == "john@gmail.com"

    def test_multiple_emails(self, detector):
        findings = detector.detect("Primary: john@gmail.com Secondary: jane@work.org")
        emails = _of_type(findings, PIIType.EMAIL)
        assert [e.matched_text for e in emails] == ["john@gmail.com", "jane@work.org"]

    def test_duplicate_email_occurrences_both_detected(self, detector):
        findings = detector.detect("john@gmail.com appears here and again john@gmail.com at the end")
        emails = _of_type(findings, PIIType.EMAIL)
        assert len(emails) == 2

    def test_no_false_positive_on_common_tech_terms(self, detector):
        findings = detector.detect("5+ years of experience with Node.js and ASP.NET")
        assert _of_type(findings, PIIType.EMAIL) == []


class TestPhoneDetection:
    @pytest.mark.parametrize("text", [
        "+91 9876543210",
        "+1 (555) 123-4567",
        "(555) 123-4567",
        "555-123-4567",
        "555 123 4567",
        "5551234567",
    ])
    def test_supported_formats(self, detector, text):
        findings = detector.detect(f"Phone: {text}")
        phones = _of_type(findings, PIIType.PHONE)
        assert len(phones) == 1, f"expected exactly one phone match for {text!r}, got {phones}"

    def test_international_country_code(self, detector):
        findings = detector.detect("Call +44 20 7946 0958 for details")
        phones = _of_type(findings, PIIType.PHONE)
        assert len(phones) == 1

    def test_extension(self, detector):
        findings = detector.detect("Reach me at +1 555 123 4567 ext 42")
        phones = _of_type(findings, PIIType.PHONE)
        assert len(phones) == 1
        assert "42" in phones[0].matched_text

    def test_multiple_phone_numbers(self, detector):
        findings = detector.detect("Mobile: 555-123-4567 Alternate: 555-987-6543")
        phones = _of_type(findings, PIIType.PHONE)
        assert len(phones) == 2

    def test_year_range_not_flagged_as_phone(self, detector):
        findings = detector.detect("Experience: 2019-2023 across three companies")
        assert _of_type(findings, PIIType.PHONE) == []

    def test_short_numeric_date_not_flagged_as_phone(self, detector):
        findings = detector.detect("Graduated 05-2020")
        assert _of_type(findings, PIIType.PHONE) == []


class TestLinkedInDetection:
    @pytest.mark.parametrize("url", [
        "linkedin.com/in/john",
        "www.linkedin.com/in/john",
        "https://linkedin.com/in/john",
        "http://linkedin.com/in/john",
        "linkedin.com/pub/john-doe/12/34/56",
    ])
    def test_formats(self, detector, url):
        findings = detector.detect(f"LinkedIn: {url}")
        linkedin = _of_type(findings, PIIType.LINKEDIN)
        assert len(linkedin) == 1

    def test_url_inside_brackets(self, detector):
        findings = detector.detect("Profile (linkedin.com/in/john) available on request")
        linkedin = _of_type(findings, PIIType.LINKEDIN)
        assert len(linkedin) == 1
        assert linkedin[0].matched_text == "linkedin.com/in/john"

    def test_trailing_sentence_punctuation_trimmed(self, detector):
        findings = detector.detect("See linkedin.com/in/john.")
        linkedin = _of_type(findings, PIIType.LINKEDIN)
        assert linkedin[0].matched_text == "linkedin.com/in/john"

    def test_not_also_classified_as_portfolio(self, detector):
        findings = detector.detect("linkedin.com/in/john")
        assert [f.pii_type for f in findings] == [PIIType.LINKEDIN]

    @pytest.mark.parametrize("url", [
        "linkedin.com/in/john",
        "https://linkedin.com/in/john",
        "www.linkedin.com/in/john",
    ])
    def test_protocol_and_www_variants_not_also_classified_as_portfolio(self, detector, url):
        # PORTFOLIO's explicit-scheme pattern matches any https?://... or
        # www.... URL unconditionally -- this asserts LINKEDIN (which scans
        # first and claims the span) always wins the overlap, regardless of
        # which URL variant is used.
        findings = detector.detect(url)
        assert [f.pii_type for f in findings] == [PIIType.LINKEDIN]


class TestGithubDetection:
    @pytest.mark.parametrize("url", [
        "github.com/johndoe",
        "www.github.com/johndoe",
        "https://github.com/johndoe",
        "http://github.com/johndoe",
    ])
    def test_formats(self, detector, url):
        findings = detector.detect(f"GitHub: {url}")
        github = _of_type(findings, PIIType.GITHUB)
        assert len(github) == 1

    def test_not_also_classified_as_portfolio(self, detector):
        findings = detector.detect("github.com/johndoe")
        assert [f.pii_type for f in findings] == [PIIType.GITHUB]

    @pytest.mark.parametrize("url", [
        "github.com/johndoe",
        "https://github.com/johndoe",
        "www.github.com/johndoe",
    ])
    def test_protocol_and_www_variants_not_also_classified_as_portfolio(self, detector, url):
        findings = detector.detect(url)
        assert [f.pii_type for f in findings] == [PIIType.GITHUB]


class TestPortfolioDetection:
    def test_https_url_always_detected(self, detector):
        findings = detector.detect("https://john.dev")
        assert len(_of_type(findings, PIIType.PORTFOLIO)) == 1

    def test_www_url_always_detected(self, detector):
        findings = detector.detect("www.johndoe.dev")
        assert len(_of_type(findings, PIIType.PORTFOLIO)) == 1

    def test_bare_domain_with_context_keyword(self, detector):
        findings = detector.detect("Portfolio: johndoe.dev")
        assert len(_of_type(findings, PIIType.PORTFOLIO)) == 1

    def test_bare_domain_without_context_not_flagged(self, detector):
        findings = detector.detect("Worked extensively with projectname.io throughout the role")
        assert _of_type(findings, PIIType.PORTFOLIO) == []

    def test_excludes_linkedin_and_github_domains(self, detector):
        findings = detector.detect("linkedin.com/in/john and github.com/john")
        assert _of_type(findings, PIIType.PORTFOLIO) == []


class TestGeneralBehavior:
    def test_empty_text(self, detector):
        assert detector.detect("") == []

    def test_whitespace_only_text(self, detector):
        assert detector.detect("   \n\t  ") == []

    def test_no_pii_present(self, detector):
        findings = detector.detect(
            "Senior Python Developer with 5 years experience in AWS, Docker, and Kubernetes"
        )
        assert findings == []

    def test_findings_sorted_by_position(self, detector):
        text = "GitHub: github.com/john Email: john@gmail.com Phone: 555-123-4567"
        findings = detector.detect(text)
        starts = [f.start for f in findings]
        assert starts == sorted(starts)

    def test_ocr_zero_whitespace_join_still_finds_email(self, detector):
        findings = detector.detect("NameJohn DoeEmail:john@gmail.comPhone:5551234567")
        assert _of_type(findings, PIIType.EMAIL) != []

    def test_candidate_name_and_skills_never_flagged(self, detector):
        findings = detector.detect(
            "John Doe is a Software Engineer skilled in React, FastAPI, and PostgreSQL"
        )
        assert findings == []


class TestBulkIdentityTieBreak:
    """
    Bulk upload's identity resolution (app/tasks/bulk_upload_tasks.py,
    _resolve_bulk_candidate_identity) takes the FIRST EMAIL/PHONE finding in
    document order when a resume lists more than one -- this is an
    intentional, documented tie-break (see that function's docstring),
    matching how a human skimming top-to-bottom would pick the "primary"
    contact. These tests pin that behavior against the exact multi-email/
    multi-phone scenarios called out during review.
    """

    def test_personal_email_before_work_email_wins(self, detector):
        text = "Personal Email: john@gmail.com\nWork Email: john@company.com"
        findings = detector.detect(text)
        emails = _of_type(findings, PIIType.EMAIL)
        assert [e.matched_text for e in emails] == ["john@gmail.com", "john@company.com"]
        # "first in document order" == emails[0], which the resolver in
        # bulk_upload_tasks.py takes via next(...).
        assert emails[0].matched_text == "john@gmail.com"

    def test_multiple_phone_numbers_first_in_document_order_wins(self, detector):
        text = "Mobile: +91 9876543210\nAlternate: +91 9123456780"
        findings = detector.detect(text)
        phones = _of_type(findings, PIIType.PHONE)
        assert len(phones) == 2
        assert phones[0].matched_text == "+91 9876543210"
