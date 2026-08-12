from app.enums.education import DegreeLevel, EducationField, EducationMatchResult
from app.services.education.education_matching_service import (
    classify_degree_level,
    classify_field,
    detect_related_field_allowed,
    evaluate_candidate_education,
    match_education,
    normalize_candidate_education_entry,
    normalize_jd_education_requirement,
)


# ---------------------------------------------------------------- classify_degree_level


def test_classify_degree_level_recognizes_bachelor_abbreviations():
    for text in ("Bachelor of Technology (B.Tech)", "B.E.", "B.Sc.", "BCA", "BA", "B.Com"):
        assert classify_degree_level(text) == DegreeLevel.BACHELOR, text


def test_classify_degree_level_recognizes_master_abbreviations():
    for text in ("M.Tech", "M.E.", "MCA", "MBA", "M.Sc."):
        assert classify_degree_level(text) == DegreeLevel.MASTER, text


def test_classify_degree_level_recognizes_doctorate():
    for text in ("PhD", "Doctor of Philosophy"):
        assert classify_degree_level(text) == DegreeLevel.DOCTORATE, text


def test_classify_degree_level_recognizes_diploma_and_associate():
    assert classify_degree_level("Diploma in Computer Science") == DegreeLevel.DIPLOMA
    assert classify_degree_level("Associate degree") == DegreeLevel.ASSOCIATE


def test_classify_degree_level_unknown_never_guessed():
    assert classify_degree_level(None) == DegreeLevel.UNKNOWN
    assert classify_degree_level("") == DegreeLevel.UNKNOWN
    assert classify_degree_level("Some Unrecognizable Qualification Xyz") == DegreeLevel.UNKNOWN


def test_classify_degree_level_picks_highest_rank_when_multiple_match():
    """"Bachelor's or Master's" mentions both - the higher-ranked MASTER should win."""
    assert classify_degree_level("Bachelor's or Master's degree") == DegreeLevel.MASTER


# ---------------------------------------------------------------- classify_field


def test_classify_field_recognizes_computer_science_variants():
    for text in ("Computer Science and Engineering", "CSE", "Computer Science"):
        assert classify_field(text) == EducationField.COMPUTER_SCIENCE, text


def test_classify_field_unknown_never_invented():
    assert classify_field(None) == EducationField.UNKNOWN
    assert classify_field("Underwater Basket Weaving") == EducationField.UNKNOWN


# ---------------------------------------------------------------- detect_related_field_allowed


def test_detect_related_field_allowed_true_for_explicit_phrases():
    assert detect_related_field_allowed("Computer Science or related field") is True
    assert detect_related_field_allowed("Computer Science or equivalent discipline") is True


def test_detect_related_field_allowed_false_when_not_mentioned():
    assert detect_related_field_allowed("Computer Science") is False
    assert detect_related_field_allowed(None) is False


# ---------------------------------------------------------------- match_education (Part 14)


def test_full_match_same_degree_level_and_field():
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, False,
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE,
    )
    assert result == EducationMatchResult.FULL_MATCH


def test_related_field_match_when_allowed_and_related():
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, True,
        DegreeLevel.BACHELOR, EducationField.INFORMATION_TECHNOLOGY,
    )
    assert result == EducationMatchResult.RELATED_FIELD_MATCH


def test_discipline_mismatch_when_related_not_allowed():
    """Same related field, but the JD did NOT explicitly allow a related field -> mismatch, not a match."""
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, False,
        DegreeLevel.BACHELOR, EducationField.INFORMATION_TECHNOLOGY,
    )
    assert result == EducationMatchResult.DISCIPLINE_MISMATCH


def test_discipline_mismatch_when_unrelated_field():
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, True,
        DegreeLevel.BACHELOR, EducationField.MECHANICAL_ENGINEERING,
    )
    assert result == EducationMatchResult.DISCIPLINE_MISMATCH


def test_degree_level_mismatch_when_below_required_level():
    """Diploma when Bachelor's required -> DEGREE_LEVEL_MISMATCH, regardless of field."""
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, False,
        DegreeLevel.DIPLOMA, EducationField.COMPUTER_SCIENCE,
    )
    assert result == EducationMatchResult.DEGREE_LEVEL_MISMATCH


def test_degree_level_exceeds_when_above_required_level_and_field_matches():
    """Master's Computer Science when Bachelor's minimum required -> DEGREE_LEVEL_EXCEEDS."""
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, False,
        DegreeLevel.MASTER, EducationField.COMPUTER_SCIENCE,
    )
    assert result == EducationMatchResult.DEGREE_LEVEL_EXCEEDS


def test_exceeding_degree_level_does_not_excuse_a_wrong_field():
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, False,
        DegreeLevel.MASTER, EducationField.MECHANICAL_ENGINEERING,
    )
    assert result == EducationMatchResult.DISCIPLINE_MISMATCH


def test_no_field_requirement_means_only_degree_level_is_checked():
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.UNKNOWN, False,
        DegreeLevel.BACHELOR, EducationField.MECHANICAL_ENGINEERING,
    )
    assert result == EducationMatchResult.FULL_MATCH


def test_unranked_levels_produce_partial_match_not_a_confident_verdict():
    result = match_education(
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE, False,
        DegreeLevel.PROFESSIONAL, EducationField.COMPUTER_SCIENCE,
    )
    assert result == EducationMatchResult.PARTIAL_MATCH


def test_unknown_requirement_never_guessed_into_a_verdict():
    result = match_education(
        DegreeLevel.UNKNOWN, EducationField.UNKNOWN, False,
        DegreeLevel.BACHELOR, EducationField.COMPUTER_SCIENCE,
    )
    assert result == EducationMatchResult.UNKNOWN


# ---------------------------------------------------------------- normalize_candidate_education_entry


def test_normalize_prefers_ai_populated_values_over_raw_text():
    entry = {"degree": "Some Weird Text", "field": "Some Weird Field", "degree_level": "BACHELOR", "field_normalized": "COMPUTER_SCIENCE"}
    normalized = normalize_candidate_education_entry(entry)
    assert normalized["degree_level"] == DegreeLevel.BACHELOR
    assert normalized["field_normalized"] == EducationField.COMPUTER_SCIENCE


def test_normalize_falls_back_to_raw_text_classifier_when_ai_values_missing():
    """Legacy resume (Part 21): no degree_level/field_normalized keys at all - never requires reprocessing."""
    entry = {"degree": "B.Tech", "field": "Computer Science"}
    normalized = normalize_candidate_education_entry(entry)
    assert normalized["degree_level"] == DegreeLevel.BACHELOR
    assert normalized["field_normalized"] == EducationField.COMPUTER_SCIENCE


def test_normalize_falls_back_when_ai_values_are_explicitly_unknown():
    entry = {"degree": "B.Tech", "field": "Computer Science", "degree_level": "UNKNOWN", "field_normalized": "UNKNOWN"}
    normalized = normalize_candidate_education_entry(entry)
    assert normalized["degree_level"] == DegreeLevel.BACHELOR
    assert normalized["field_normalized"] == EducationField.COMPUTER_SCIENCE


# ---------------------------------------------------------------- normalize_jd_education_requirement


def test_normalize_jd_requirement_from_recruiter_free_text():
    requirement = normalize_jd_education_requirement({"degree": "Bachelor's degree", "field": "Computer Science or related field"})
    assert requirement["degree_level"] == DegreeLevel.BACHELOR
    assert requirement["field_normalized"] == EducationField.COMPUTER_SCIENCE
    assert requirement["related_field_allowed"] is True


def test_normalize_jd_requirement_handles_missing_criteria():
    requirement = normalize_jd_education_requirement(None)
    assert requirement["degree_level"] == DegreeLevel.UNKNOWN
    assert requirement["field_normalized"] == EducationField.UNKNOWN
    assert requirement["related_field_allowed"] is False


# ---------------------------------------------------------------- evaluate_candidate_education (end-to-end)


def test_evaluate_full_match_end_to_end():
    result = evaluate_candidate_education(
        {"degree": "Bachelor's degree", "field": "Computer Science"},
        [{"degree": "B.Tech", "field": "Computer Science"}],
    )
    assert result["result"] == EducationMatchResult.FULL_MATCH.value
    assert result["candidate_degree_level"] == DegreeLevel.BACHELOR.value
    assert result["matched_entry_index"] == 0


def test_evaluate_related_field_match_end_to_end():
    result = evaluate_candidate_education(
        {"degree": "Bachelor's degree", "field": "Computer Science or related field"},
        [{"degree": "B.Tech", "field": "Information Technology"}],
    )
    assert result["result"] == EducationMatchResult.RELATED_FIELD_MATCH.value


def test_evaluate_discipline_mismatch_end_to_end():
    result = evaluate_candidate_education(
        {"degree": "Bachelor's degree", "field": "Computer Science"},
        [{"degree": "B.Tech", "field": "Mechanical Engineering"}],
    )
    assert result["result"] == EducationMatchResult.DISCIPLINE_MISMATCH.value


def test_evaluate_degree_level_mismatch_end_to_end():
    result = evaluate_candidate_education(
        {"degree": "Bachelor's degree", "field": "Computer Science"},
        [{"degree": "Diploma", "field": "Computer Science"}],
    )
    assert result["result"] == EducationMatchResult.DEGREE_LEVEL_MISMATCH.value


def test_evaluate_degree_level_exceeds_end_to_end():
    result = evaluate_candidate_education(
        {"degree": "Bachelor's degree", "field": "Computer Science"},
        [{"degree": "Master of Technology", "field": "Computer Science"}],
    )
    assert result["result"] == EducationMatchResult.DEGREE_LEVEL_EXCEEDS.value


def test_evaluate_no_education_data_when_candidate_has_no_entries():
    result = evaluate_candidate_education({"degree": "Bachelor's degree", "field": "Computer Science"}, [])
    assert result["result"] == EducationMatchResult.NO_EDUCATION_DATA.value


def test_evaluate_unknown_when_jd_has_no_parseable_requirement():
    result = evaluate_candidate_education(None, [{"degree": "B.Tech", "field": "Computer Science"}])
    assert result["result"] == EducationMatchResult.UNKNOWN.value


def test_evaluate_picks_the_best_matching_entry_among_several():
    """Candidate has Bachelor's-Mechanical AND Master's-CS - the CS one should win against a Bachelor's-CS requirement."""
    result = evaluate_candidate_education(
        {"degree": "Bachelor's degree", "field": "Computer Science"},
        [
            {"degree": "B.Tech", "field": "Mechanical Engineering"},
            {"degree": "M.Tech", "field": "Computer Science"},
        ],
    )
    assert result["result"] == EducationMatchResult.DEGREE_LEVEL_EXCEEDS.value
    assert result["matched_entry_index"] == 1
