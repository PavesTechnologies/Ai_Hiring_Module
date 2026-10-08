"""
Scoring v2 hooks in ResumeProcessingPipeline: the parse cache (read only when
enabled, bypassed on re-parse, stored under the model that answered) and the
evidence step (never able to affect the v1 parse).
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline, parse_cache_key
from app.services.skills.skill_normalization_service import SkillMatchResult, SkillMatchTier

PROMPT = SimpleNamespace(template_text="prompt", content_hash="ph")


def _pipeline(cached=None, answered_by="main", used_fallback=False):
    provider = SimpleNamespace(provider_name="GROQ", model="main", last_model_used=answered_by,
                               last_used_fallback=used_fallback)
    extraction = MagicMock(provider=provider)
    extraction.extract_raw.return_value = {"skills": ["fresh"]}
    repo = MagicMock()
    repo.get_parse_cache.return_value = cached
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.extraction_service = extraction
    pipeline.prompt_template_repository = MagicMock(get_by_id=MagicMock(return_value=PROMPT))
    pipeline.detection_repository = repo
    pipeline.skill_evidence_service = None
    return pipeline, extraction, repo


def _context(**kw):
    return SimpleNamespace(redacted_text="resume text", prompt_template_id=uuid4(), raw_extraction=None,
                           parse_call=None, parse_cache_enabled=False, bypass_parse_cache=False, **kw)


CACHED = SimpleNamespace(raw_extraction={"skills": ["cached"]}, provider="GROQ", model="main")


def test_cache_not_read_when_disabled_but_answer_stored():
    pipeline, extraction, repo = _pipeline(cached=CACHED)
    context = _context()
    pipeline._run_ai_extraction(context)
    repo.get_parse_cache.assert_not_called()
    assert context.raw_extraction == {"skills": ["fresh"]}
    stored = repo.put_parse_cache.call_args.kwargs
    assert stored["cache_key"] == parse_cache_key("resume text", "ph", "GROQ", "main")
    assert context.parse_call == {"provider": "GROQ", "model": "main", "used_fallback": False, "cache_hit": False}


def test_cache_hit_when_enabled_skips_the_llm():
    pipeline, extraction, repo = _pipeline(cached=CACHED)
    context = _context()
    context.parse_cache_enabled = True
    pipeline._run_ai_extraction(context)
    extraction.extract_raw.assert_not_called()
    assert context.raw_extraction == {"skills": ["cached"]}
    assert context.parse_call["cache_hit"] is True


def test_reparse_bypasses_the_cache_and_replaces_it():
    pipeline, extraction, repo = _pipeline(cached=CACHED)
    context = _context()
    context.parse_cache_enabled, context.bypass_parse_cache = True, True
    pipeline._run_ai_extraction(context)
    repo.get_parse_cache.assert_not_called()
    extraction.extract_raw.assert_called_once()
    repo.put_parse_cache.assert_called_once()


def test_backup_model_answer_is_keyed_by_the_backup_model():
    pipeline, _, repo = _pipeline(answered_by="backup", used_fallback=True)
    context = _context()
    pipeline._run_ai_extraction(context)
    assert repo.put_parse_cache.call_args.kwargs["cache_key"] == parse_cache_key("resume text", "ph", "GROQ", "backup")
    assert context.parse_call["used_fallback"] is True and context.parse_call["model"] == "backup"


def test_cache_key_changes_with_text_prompt_model_and_detection_version(monkeypatch):
    base = parse_cache_key("t", "p", "GROQ", "m")
    assert len({base, parse_cache_key("t2", "p", "GROQ", "m"), parse_cache_key("t", "p2", "GROQ", "m"),
                parse_cache_key("t", "p", "GROQ", "m2")}) == 4
    import app.services.resume.resume_processing_pipeline as module
    monkeypatch.setattr(module, "DETECTION_VERSION", "next")
    assert parse_cache_key("t", "p", "GROQ", "m") != base


def test_cache_failure_never_breaks_the_parse():
    pipeline, extraction, repo = _pipeline()
    repo.put_parse_cache.side_effect = RuntimeError("db down")
    context = _context()
    pipeline._run_ai_extraction(context)
    assert context.raw_extraction == {"skills": ["fresh"]}


def test_v2_detection_failure_is_contained():
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.skill_evidence_service = MagicMock()
    pipeline.skill_evidence_service.detect.side_effect = RuntimeError("boom")
    pipeline.resume_repository = MagicMock()
    match = SkillMatchResult("Python", True, uuid4(), SkillMatchTier.EXACT, 1.0)
    context = SimpleNamespace(resume_id=uuid4(), redacted_text="Python", skill_match_results=[match],
                              parse_call={"provider": "GROQ", "model": "m", "used_fallback": False})
    pipeline._run_skill_detection_v2(context)          # does not raise
    pipeline.resume_repository.rollback.assert_called_once()
    pipeline.resume_repository.commit.assert_not_called()


def test_v2_detection_passes_llm_matches_and_parse_model():
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.skill_evidence_service = MagicMock()
    pipeline.resume_repository = MagicMock()
    known, unknown = SkillMatchResult("Python", True, uuid4(), SkillMatchTier.EXACT, 1.0), \
        SkillMatchResult("Zzz", True, None, SkillMatchTier.UNKNOWN, None)
    context = SimpleNamespace(resume_id=uuid4(), redacted_text="Python", skill_match_results=[known, unknown],
                              parse_call={"provider": "GROQ", "model": "backup", "used_fallback": True})
    pipeline._run_skill_detection_v2(context)
    kwargs = pipeline.skill_evidence_service.detect.call_args.kwargs
    assert [m.raw_text for m in kwargs["llm_matches"]] == ["Python"]
    assert (kwargs["parse"].model, kwargs["parse"].used_fallback) == ("backup", True)
    pipeline.resume_repository.commit.assert_called_once()
