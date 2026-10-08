from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.schemas.ai.resume_extraction_response import ResumeExtractionResponse
from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline
from app.services.skills.skill_normalization_service import SkillMatchResult, SkillMatchTier

"""
Resume skill normalization adds (a) parts of compound skills and (b)
evidence-backed inferred skills - only when they resolve to a known catalog
skill the resume doesn't already hold, so neither can add unknown-skill
noise. The original skills normalize exactly as before.
"""

CATALOG = {"program management": uuid4(), "change management": uuid4(), "digital transformation": uuid4(),
           "ai transformation": uuid4(), "python": uuid4()}


def _match(name):
    skill_id = CATALOG.get(name.lower())
    return SkillMatchResult(
        raw_text=name, mandatory=True, canonical_skill_id=skill_id,
        match_tier=SkillMatchTier.EXACT if skill_id else SkillMatchTier.UNKNOWN,
        confidence=1.0 if skill_id else None, normalized_text=name.lower(),
    )


def _pipeline():
    normalizer = MagicMock()
    normalizer.normalize_skills.side_effect = lambda required_skills, preferred_skills: [
        _match(n) for n in required_skills
    ]
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.skill_normalization_service = normalizer
    return pipeline, normalizer


def _run(extraction: dict):
    pipeline, normalizer = _pipeline()
    context = SimpleNamespace(validated_extraction=ResumeExtractionResponse.model_validate(extraction))
    pipeline._run_skill_normalization(context)
    return context.skill_match_results, normalizer


def test_compound_parts_add_only_known_skills_with_their_origin():
    results, _ = _run({"skills": ["Python", "IT Project & Program Management", "AI & Digital Transformation"]})

    matched = {r.raw_text: r.canonical_skill_id for r in results if r.canonical_skill_id}
    assert matched['Program Management (from "IT Project & Program Management")'] == CATALOG["program management"]
    assert matched['AI Transformation (from "AI & Digital Transformation")'] == CATALOG["ai transformation"]
    assert matched['Digital Transformation (from "AI & Digital Transformation")'] == CATALOG["digital transformation"]
    # "IT Project Management" isn't in the catalog - dropped, never an unknown-skill row.
    assert not any("IT Project Management" in r.raw_text for r in results)
    # The original phrases still go through exactly as before (unmatched as a whole).
    assert [r.raw_text for r in results[:3]] == ["Python", "IT Project & Program Management", "AI & Digital Transformation"]


def test_inferred_skill_with_evidence_counts_and_records_the_evidence():
    results, _ = _run({
        "skills": ["Python"],
        "inferred_skills": [
            {"skill": "Program Management", "evidence": "PMP"},
            {"skill": "Unicorn Wrangling", "evidence": "Head of unicorns"},     # unknown - dropped
            {"skill": "Change Management", "evidence": None},                   # no evidence - never counts
        ],
    })

    assert [(r.raw_text, r.canonical_skill_id) for r in results[1:]] == [
        ("Program Management (inferred: PMP)", CATALOG["program management"]),
    ]


def test_nothing_is_added_twice_or_when_already_held():
    results, _ = _run({
        "skills": ["Program Management", "IT Project & Program Management"],
        "inferred_skills": [{"skill": "Program Management", "evidence": "PgMP"}],
    })

    held = [r.canonical_skill_id for r in results if r.canonical_skill_id]
    assert held.count(CATALOG["program management"]) == 1


def test_plain_resume_makes_a_single_normalization_call():
    results, normalizer = _run({"skills": ["Python", "Docker"]})

    assert normalizer.normalize_skills.call_count == 1
    assert [r.raw_text for r in results] == ["Python", "Docker"]


def test_inferred_skills_validation_drops_unevidenced_and_duplicates():
    extraction = ResumeExtractionResponse.model_validate({"inferred_skills": [
        {"skill": "Program Management", "evidence": "PMP"},
        {"skill": "program management", "evidence": "PgMP"},
        {"skill": "Risk Management", "evidence": "  "},
        {"skill": None, "evidence": "x"},
    ]})
    assert [(i.skill, i.evidence) for i in extraction.inferred_skills] == [("Program Management", "PMP")]


def test_fuzzy_or_semantic_near_misses_never_add_a_skill():
    pipeline, normalizer = _pipeline()

    def normalize(required_skills, preferred_skills):
        out = []
        for name in required_skills:
            if name == "Financial Portfolio Governance":
                # e.g. the fuzzy tier resolving a fragment to an unrelated JD skill
                out.append(SkillMatchResult(raw_text=name, mandatory=True, canonical_skill_id=uuid4(),
                                            match_tier=SkillMatchTier.FUZZY, confidence=0.7))
            else:
                out.append(_match(name))
        return out

    normalizer.normalize_skills.side_effect = normalize
    context = SimpleNamespace(validated_extraction=ResumeExtractionResponse.model_validate(
        {"skills": ["P&L & Financial Portfolio Governance"]}))
    pipeline._run_skill_normalization(context)

    assert [r.raw_text for r in context.skill_match_results] == ["P&L & Financial Portfolio Governance"]


# ---------------------------------------------------------------- evidence must be in the resume

from app.services.resume.resume_processing_pipeline import evidence_found_in  # noqa: E402

RESUME = """Senior Director, Technical Program Management - Visa
Governed a $458M enterprise Data & AI Platform portfolio at Visa.
Certifications: Project Management Professional (PMP); Six Sigma Green Belt"""


def test_evidence_is_matched_ignoring_case_punctuation_and_spacing():
    assert evidence_found_in("governed a $458M enterprise data & AI platform portfolio", RESUME)
    assert evidence_found_in("Senior Director,  Technical Program Management", RESUME)
    assert evidence_found_in("PMP", RESUME)


def test_fabricated_or_empty_evidence_is_rejected():
    assert not evidence_found_in("Led enterprise-wide change adoption for 5,000 users", RESUME)
    assert not evidence_found_in("", RESUME)
    assert not evidence_found_in("PM", RESUME)                 # too short to mean anything
    assert not evidence_found_in("PMP", None)                  # nothing to verify against
    assert not evidence_found_in("Visa", "Visas and permits")  # whole words only


def test_json_validation_drops_inferences_whose_evidence_is_not_in_the_resume():
    pipeline, _ = _pipeline()
    context = SimpleNamespace(redacted_text=RESUME, resume_id=uuid4(), raw_extraction={"inferred_skills": [
        {"skill": "Program Management", "evidence": "Senior Director, Technical Program Management"},
        {"skill": "Change Management", "evidence": "Led enterprise-wide change adoption for 5,000 users"},
    ]})

    pipeline._run_json_validation(context)

    assert [i.skill for i in context.validated_extraction.inferred_skills] == ["Program Management"]


def test_long_real_quotes_split_across_lines_are_found():
    """Real cases: PDF text wraps mid-sentence, and quotes run past 60 characters."""
    resume = ("platforms. Governed a $458M enterprise\nData & AI Platform portfolio at Visa and grew "
              "technology program management\nPresented weekly/monthly progress to Steer Co and senior "
              "client leadership, surfacing risks")
    assert evidence_found_in("Governed a $458M enterprise Data & AI Platform portfolio at Visa", resume)
    assert evidence_found_in(
        "Presented weekly/monthly progress to Steer Co and senior client leadership, surfacing risks", resume)
    # An added word the resume doesn't contain is still a mismatch.
    assert not evidence_found_in("Received Governed a $458M enterprise Data & AI Platform portfolio", resume)
