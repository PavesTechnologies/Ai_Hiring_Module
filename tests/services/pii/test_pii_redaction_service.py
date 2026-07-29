from app.services.pii.pii_detection_service import PIIDetectionService
from app.services.pii.pii_redaction_service import PIIRedactionService
from app.services.pii.pii_types import PIIFinding, PIIType


def _redact(text: str) -> str:
    findings = PIIDetectionService().detect(text)
    return PIIRedactionService().redact(text, findings)


def test_email_redacted_to_placeholder():
    assert _redact("Email: john@gmail.com") == "Email: [EMAIL]"


def test_phone_redacted_to_placeholder():
    assert _redact("Phone: +91 9876543210") == "Phone: [PHONE]"


def test_linkedin_redacted_to_placeholder():
    assert _redact("LinkedIn: linkedin.com/in/john") == "LinkedIn: [LINKEDIN]"


def test_github_redacted_to_placeholder():
    assert _redact("GitHub: github.com/john") == "GitHub: [GITHUB]"


def test_portfolio_redacted_to_placeholder():
    assert _redact("Portfolio: https://john.dev") == "Portfolio: [PORTFOLIO]"


def test_all_types_redacted_in_one_pass_preserving_structure():
    text = (
        "Email: john@gmail.com\n"
        "Phone: +91 9876543210\n"
        "LinkedIn: linkedin.com/in/john"
    )
    expected = (
        "Email: [EMAIL]\n"
        "Phone: [PHONE]\n"
        "LinkedIn: [LINKEDIN]"
    )
    assert _redact(text) == expected


def test_every_occurrence_redacted_not_just_first():
    text = "Contact john@gmail.com or john@gmail.com again"
    redacted = _redact(text)
    assert redacted.count("[EMAIL]") == 2
    assert "@" not in redacted


def test_no_pii_returns_text_unchanged():
    text = "Senior Python Developer with 5 years of experience"
    assert _redact(text) == text


def test_no_findings_short_circuits_to_original_text():
    text = "nothing to redact here"
    assert PIIRedactionService().redact(text, []) is text


def test_non_pii_text_between_redactions_untouched():
    text = "Name: John Doe. Skills: Python, FastAPI. Email: john@gmail.com. Location: Remote."
    redacted = _redact(text)
    assert redacted.startswith("Name: John Doe. Skills: Python, FastAPI. Email: [EMAIL].")
    assert redacted.endswith("Location: Remote.")


def test_manual_findings_redact_in_document_order_regardless_of_input_order():
    text = "aaaEMAILbbbPHONEccc"
    findings = [
        PIIFinding(pii_type=PIIType.PHONE, start=11, end=16, matched_text="PHONE"),
        PIIFinding(pii_type=PIIType.EMAIL, start=3, end=8, matched_text="EMAIL"),
    ]
    assert PIIRedactionService().redact(text, findings) == "aaa[EMAIL]bbb[PHONE]ccc"
