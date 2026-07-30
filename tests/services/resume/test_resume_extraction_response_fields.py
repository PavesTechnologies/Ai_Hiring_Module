from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse


def test_department_and_location_are_preserved_for_parsed_json():
    extraction = ResumeExtractionResponse.model_validate(
        {
            "full_name": "Jane Doe",
            "skills": ["Python"],
            "department": "  Engineering  ",
            "location": "  Hyderabad, India  ",
            "summary": "Backend engineer",
        }
    )

    parsed_json = extraction.model_dump(mode="json")

    assert extraction.department == "Engineering"
    assert extraction.location == "Hyderabad, India"
    assert parsed_json["department"] == "Engineering"
    assert parsed_json["location"] == "Hyderabad, India"

