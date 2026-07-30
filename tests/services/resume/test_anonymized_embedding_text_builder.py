from app.services.resume.anonymized_embedding_text_builder import (
    build_anonymized_embedding_text,
    verify_anonymized_text,
)


def test_builds_text_from_skills_experience_and_education():
    parsed_json = {
        "skills": ["Python", "SQL"],
        "work_experience": [
            {"title": "Engineer", "company": "Acme", "start_date": "2019", "end_date": "2022"},
        ],
        "education": [{"degree": "Bachelor's", "field": "CS", "institution": "State University"}],
    }

    text = build_anonymized_embedding_text(parsed_json)

    assert "Skills: Python, SQL" in text
    assert "Experience: Engineer at Acme (2019-2022)" in text
    assert "Education: Bachelor's CS State University" in text


def test_returns_empty_string_when_nothing_present():
    assert build_anonymized_embedding_text({}) == ""
    assert build_anonymized_embedding_text({"skills": [], "work_experience": [], "education": []}) == ""


def test_ignores_never_reads_pii_style_keys():
    # These keys don't exist on the real schema, but even if present on a
    # malformed dict, the builder must never surface them.
    parsed_json = {
        "skills": ["Python"],
        "full_name": "Jane Doe",
        "email": "jane@example.com",
    }

    text = build_anonymized_embedding_text(parsed_json)

    assert "Jane" not in text
    assert "jane@example.com" not in text


def test_verify_passes_clean_text():
    is_valid, reason = verify_anonymized_text("Skills: Python, SQL\nExperience: Engineer at Acme (2019-2022)")
    assert is_valid is True
    assert reason is None


def test_verify_flags_email_address():
    is_valid, reason = verify_anonymized_text("Skills: Python\ncontact jane@example.com")
    assert is_valid is False
    assert "email" in reason.lower()


def test_verify_flags_url():
    is_valid, reason = verify_anonymized_text("Skills: Python\nhttps://linkedin.com/in/janedoe")
    assert is_valid is False
    assert "url" in reason.lower()


def test_verify_flags_phone_number():
    is_valid, reason = verify_anonymized_text("Skills: Python\nCall +1-555-123-4567")
    assert is_valid is False
    assert "phone" in reason.lower()


def test_verify_does_not_false_positive_on_year_ranges():
    is_valid, reason = verify_anonymized_text("Experience: Engineer at Acme (2019-2022)")
    assert is_valid is True
    assert reason is None
