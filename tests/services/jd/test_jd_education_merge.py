from app.services.jd.jd_education_merge import (
    GAP_FILLED_KEY,
    merge_education_with_criteria,
    merge_extracted_json_education,
)

"""
JD extracted_json education: AI extraction first, the recruiter's upload-time
education_criteria fills whatever it left empty, never null.
"""

FORM = {"degree": "B.Tech", "field": "Computer Science or related field"}


def test_null_education_is_built_from_form():
    education = merge_education_with_criteria(None, FORM)

    assert education["degree"] == "B.Tech"
    assert education["field"] == "Computer Science or related field"
    assert education["degree_level"] == "BACHELOR"
    assert education["field_normalized"] == "COMPUTER_SCIENCE"
    assert education["related_field_allowed"] is True
    assert set(education[GAP_FILLED_KEY]) == {
        "degree", "field", "degree_level", "field_normalized", "related_field_allowed",
    }


def test_ai_values_are_never_overwritten():
    ai = {"degree": "Master's degree", "field": "Data Science", "degree_level": "MASTER",
          "field_normalized": "DATA_SCIENCE", "related_field_allowed": False}

    education = merge_education_with_criteria(ai, FORM)

    assert education == ai  # nothing was a gap, so no gap_filled_fields either


def test_only_partial_gaps_are_filled():
    ai = {"degree": "Bachelor's degree", "field": None, "degree_level": "BACHELOR",
          "field_normalized": "UNKNOWN", "related_field_allowed": False}

    education = merge_education_with_criteria(ai, {"degree": "M.Tech", "field": "Information Technology"})

    assert education["degree"] == "Bachelor's degree"          # AI kept
    assert education["degree_level"] == "BACHELOR"             # AI kept
    assert education["field"] == "Information Technology"      # filled
    assert education["field_normalized"] == "INFORMATION_TECHNOLOGY"
    assert education[GAP_FILLED_KEY] == ["field", "field_normalized"]


def test_unknown_level_classified_from_ai_text_when_form_empty():
    ai = {"degree": "B.E. in any discipline", "degree_level": "UNKNOWN"}

    education = merge_education_with_criteria(ai, {"degree": None, "field": None})

    assert education["degree_level"] == "BACHELOR"
    assert education[GAP_FILLED_KEY] == ["degree_level"]


def test_never_null_even_with_no_data_anywhere():
    education = merge_education_with_criteria(None, None)

    assert education == {
        "degree": None, "field": None, "degree_level": "UNKNOWN",
        "field_normalized": "UNKNOWN", "related_field_allowed": False,
    }


def test_edited_form_refills_previous_gap_fills():
    first = merge_education_with_criteria(None, {"degree": "B.Tech", "field": "Computer Science"})

    second = merge_education_with_criteria(first, {"degree": "MBA", "field": "Business Administration"})

    assert second["degree"] == "MBA"
    assert second["degree_level"] == "MASTER"
    assert second["field_normalized"] == "BUSINESS_ADMINISTRATION"


def test_edit_does_not_touch_ai_values():
    ai = {"degree": "Bachelor's degree", "degree_level": "BACHELOR", "field": None}
    first = merge_education_with_criteria(ai, {"field": "Computer Science"})

    second = merge_education_with_criteria(first, {"degree": "PhD", "field": "Mathematics"})

    assert second["degree"] == "Bachelor's degree"
    assert second["degree_level"] == "BACHELOR"
    assert second["field"] == "Mathematics"
    assert second["field_normalized"] == "MATHEMATICS"


def test_invalid_ai_enum_values_are_treated_as_gaps():
    education = merge_education_with_criteria({"degree_level": "GRADUATE"}, {"degree": "Bachelor of Science"})

    assert education["degree_level"] == "BACHELOR"


def test_extracted_json_wrapper_keeps_other_keys_and_none():
    assert merge_extracted_json_education(None, FORM) is None

    merged = merge_extracted_json_education({"title": "Dev", "education": None}, FORM)

    assert merged["title"] == "Dev"
    assert merged["education"]["degree_level"] == "BACHELOR"
