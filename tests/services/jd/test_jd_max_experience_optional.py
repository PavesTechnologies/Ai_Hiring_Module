"""
Review item 27: a JD's maximum experience acts as an age filter, so it is optional and nothing that
judges a candidate sees it.

- job_descriptions.max_experience_years is nullable; the create/update requests and both file forms
  accept no maximum;
- the JD embedding text states only the minimum;
- the AI evaluator's JD input drops it (item 6; tests/services/campaign/test_ai_evaluation_input.py).
"""
import inspect

import pytest

from app.schemas.ai.jd_extraction_response import JDExtractionResponse
from app.schemas.jd.request import CreateJDRequest, UpdateJDRequest

BASE = {"title": "Backend engineer", "raw_text": "We need Python.", "jurisdiction": "IN",
        "min_experience_years": 3, "notice_period": 30, "education_criteria": {},
        "prompt_template_id": "00000000-0000-0000-0000-000000000001"}


@pytest.mark.parametrize("model", [CreateJDRequest, UpdateJDRequest])
def test_a_jd_can_be_saved_without_a_maximum(model):
    assert model(**BASE).max_experience_years is None
    assert model(**BASE, max_experience_years=8).max_experience_years == 8


@pytest.mark.parametrize("model", [CreateJDRequest, UpdateJDRequest])
def test_a_maximum_below_the_minimum_is_still_refused(model):
    with pytest.raises(ValueError):
        model(**BASE, max_experience_years=1)


def test_the_column_is_nullable():
    from app.models.jd.job_descriptions import JobDescription
    assert JobDescription.__table__.c.max_experience_years.nullable is True


def test_the_file_forms_default_to_no_maximum():
    from app.api.routes import jd_routes
    for route in (jd_routes.create_job_description_from_file, jd_routes.update_job_description_from_file):
        default = inspect.signature(route).parameters["max_experience_years"].default
        assert getattr(default, "default", "required") is None


def _extraction(min_years, max_years):
    return JDExtractionResponse.model_validate({
        "required_skills": {"core": ["Python"], "supporting": []},
        "experience": {"min_experience_years": min_years, "max_experience_years": max_years},
    })


@pytest.mark.parametrize("min_years, max_years, line", [
    (3, 8, "Experience: 3+ years"),
    (3, None, "Experience: 3+ years"),
    (2.5, 10, "Experience: 2.5+ years"),
])
def test_the_embedding_text_states_only_the_minimum(min_years, max_years, line):
    from app.services.ai.embedding_service import EmbeddingService
    text = EmbeddingService.build_canonical_embedding_text(_extraction(min_years, max_years), "Backend engineer")
    experience = [row for row in text.split("\n") if row.startswith("Experience")]
    assert experience == [line]


def test_no_experience_line_without_a_minimum():
    from app.services.ai.embedding_service import EmbeddingService
    text = EmbeddingService.build_canonical_embedding_text(_extraction(None, 12), "Backend engineer")
    assert "Experience" not in text and "12" not in text
