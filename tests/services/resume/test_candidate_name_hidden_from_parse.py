"""
The candidate's name is found before the parse (header line, else a small LLM call on the header)
and hidden from the text the parse sees; the parse's full_name is then set from it - the typed name
for single uploads, the header name for bulk.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.services.resume.candidate_name import detect_header_name, find_candidate_name, header_lines
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline

RESUME = """Ananya Krishnan
[EMAIL] · [PHONE] · Chennai
Summary
Ananya Krishnan is a backend developer. Ananya built REST APIs.
Experience
Backend Developer · Acme · Jan 2024 – Present
- Wrote SQL queries and Alembic migrations on PostgreSQL
Skills
Python, Ruby, SQL"""


@pytest.mark.parametrize("lines, name", [
    (["Ananya Krishnan", "[EMAIL] · [PHONE]"], "Ananya Krishnan"),
    (["RESUME", "MOHAMMED IRFAN KHAN", "Pune"], "Mohammed Irfan Khan"),
    (["Name: Lalremruati Hmar", "Aizawl"], "Lalremruati Hmar"),
    (["Arvind Kulkarni · Pune"], "Arvind Kulkarni"),
    (["Curriculum Vitae", "R. K. Narayan"], "R. K. Narayan"),
    (["Priya O'Neil-Raman | Data Engineer"], "Priya O'Neil-Raman"),
    (["Zoë Fernandes"], "Zoë Fernandes"),
    (["K Srinivas", "Hyderabad"], "K Srinivas"),
])
def test_a_header_name_line_is_found(lines, name):
    assert detect_header_name(lines) == name


@pytest.mark.parametrize("lines", [
    ["Senior Data Engineer", "Summary"],
    ["Curriculum Vitae", "Professional Summary"],
    ["[EMAIL] · [PHONE]", "Bengaluru 560001"],
    ["Acme Technologies Pvt Ltd"],
    ["Python Developer"],
    ["Experience", "Education"],
    ["Madhu"],                                            # one word is not enough to tell
    ["B. Tech", "M. Sc"],                                 # degrees with an initial
])
def test_lines_that_are_not_a_persons_name_are_not_taken(lines):
    assert detect_header_name(lines) is None


def _service(answer):
    return MagicMock(generate_structured=MagicMock(return_value=answer))


def test_without_a_name_line_one_small_call_reads_the_header_only():
    text = "PROFILE\nsenior data engineer\nmadhu sudhan reddy, hyderabad\n" + "x\n" * 50 + "LATE LINE"
    service = _service({"full_name": "madhu sudhan reddy"})
    assert find_candidate_name(text, service) == ("madhu sudhan reddy", "llm")
    prompt = service.generate_structured.call_args.args[0]
    assert "madhu sudhan reddy" in prompt and "LATE LINE" not in prompt


@pytest.mark.parametrize("answer", [{"full_name": "Someone Else"}, {"full_name": None}, {"full_name": "[candidate]"},
                                    "not a dict"])
def test_the_calls_answer_must_be_in_the_header(answer):
    assert find_candidate_name("PROFILE\nsenior data engineer\nmadhu sudhan reddy", _service(answer)) == (None, "none")


def test_the_header_line_wins_without_any_call():
    service = _service({"full_name": "x"})
    assert find_candidate_name(RESUME, service) == ("Ananya Krishnan", "header")
    service.generate_structured.assert_not_called()
    assert header_lines("\n\n a \n b ") == ["a", "b"]


# --- in the pipeline -------------------------------------------------------------------------

PROMPT = SimpleNamespace(template_text="prompt", content_hash="ph")


def _pipeline(full_name_answer="[candidate]", catalog=("Ruby", "Python", "SQL")):
    extraction = MagicMock(provider=SimpleNamespace(provider_name="GOOGLE", model="m"))
    extraction.extract_raw.return_value = {"full_name": full_name_answer, "skills": ["Python"],
                                           "inferred_skills": [{"skill": "API Design",
                                                                "evidence": "[candidate] built REST APIs"}]}
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.extraction_service = extraction
    pipeline.prompt_template_repository = MagicMock(get_by_id=MagicMock(return_value=PROMPT))
    pipeline.detection_repository = None
    pipeline.skill_evidence_service = None
    pipeline.skill_repository = MagicMock(list_active_skills=MagicMock(return_value=[
        SimpleNamespace(id=uuid4(), canonical_name=c, aliases=[]) for c in catalog]))
    pipeline.skill_normalization_service = SimpleNamespace(skill_repository=pipeline.skill_repository,
                                                           cache_service=None)
    return pipeline, extraction


def _context(text=RESUME, known_name=None):
    return SimpleNamespace(redacted_text=text, prompt_template_id=uuid4(), raw_extraction=None, parse_call=None,
                           parse_cache_enabled=False, bypass_parse_cache=False, resume_id=uuid4(),
                           known_name=known_name, candidate_name=None, name_source=None, parse_text=None)


def test_the_parse_never_sees_the_name_and_full_name_comes_from_the_header():
    pipeline, extraction = _pipeline()
    context = _context()
    pipeline._run_ai_extraction(context)
    sent = extraction.extract_raw.call_args.args[0]
    assert "Ananya" not in sent and "Krishnan" not in sent
    assert sent.startswith("[candidate]\n") and "[candidate] is a backend developer. [candidate] built" in sent
    assert "Python, Ruby, SQL" in sent                      # skills untouched
    assert context.redacted_text == RESUME                  # the stored text keeps the name (hash, v2 evidence)

    pipeline._run_json_validation(context)
    extraction_result = context.validated_extraction
    assert extraction_result.full_name == "Ananya Krishnan" and context.name_source == "header"
    # inferred-skill evidence is checked against the text the model saw
    assert [i.skill for i in extraction_result.inferred_skills] == ["API Design"]


def test_a_single_upload_keeps_the_typed_name_and_hides_both_forms():
    text = RESUME.replace("Ananya Krishnan\n", "ANANYA K.\n", 1)
    pipeline, extraction = _pipeline()
    context = _context(text, known_name="Ananya Krishnan")
    pipeline._run_ai_extraction(context)
    sent = extraction.extract_raw.call_args.args[0]
    assert "Ananya" not in sent and "ANANYA" not in sent and "Krishnan" not in sent
    pipeline._run_json_validation(context)
    assert context.validated_extraction.full_name == "Ananya Krishnan"


def test_a_name_part_that_is_a_skill_stays_where_it_stands_alone():
    text = "Ruby Sharma\n[EMAIL]\nSkills\nRuby, Rails, Sharma-built tooling"
    pipeline, extraction = _pipeline()
    context = _context(text)
    pipeline._run_ai_extraction(context)
    sent = extraction.extract_raw.call_args.args[0]
    assert sent.startswith("[candidate]\n") and "Ruby, Rails" in sent and "Sharma" not in sent


def test_without_any_name_the_text_goes_as_is_and_the_models_name_stands_unless_it_is_the_placeholder():
    text = "senior data engineer\n[EMAIL]\nSkills\nPython"
    pipeline, extraction = _pipeline(full_name_answer="[candidate]")
    extraction.generate_structured.return_value = {"full_name": None}
    context = _context(text)
    pipeline._run_ai_extraction(context)
    assert extraction.extract_raw.call_args.args[0] == text and context.name_source == "none"
    pipeline._run_json_validation(context)
    assert context.validated_extraction.full_name is None

    pipeline, extraction = _pipeline(full_name_answer="Real Person")
    extraction.generate_structured.return_value = {"full_name": None}
    context = _context(text)
    pipeline._run_ai_extraction(context)
    pipeline._run_json_validation(context)
    assert context.validated_extraction.full_name == "Real Person"


def test_the_typed_name_is_read_from_the_candidate_and_a_failure_never_blocks():
    from app.tasks import resume_processing_tasks as tasks
    db = MagicMock()
    db.get.return_value = None
    assert tasks._stored_candidate_name(db, uuid4()) is None
    db.get.side_effect = RuntimeError("db down")
    assert tasks._stored_candidate_name(db, uuid4()) is None
