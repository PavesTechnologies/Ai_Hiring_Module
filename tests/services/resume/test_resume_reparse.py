"""Scoring v2 Step 3: re-parse & re-score - request rules, kept previous version, diff, persistence, re-score path."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage
from app.models.scoring_v2 import ResumeParseVersion
from app.services.resume.resume_reparse_service import ResumeReparseService, build_parse_diff

PM, CM, AT, ALIAS = (str(uuid4()) for _ in range(4))
CORE = [{"name": "Program Management", "ids": [PM, ALIAS]}, {"name": "Change Management", "ids": [CM]},
        {"name": "AI Transformation", "ids": [AT]}]


def _skill(skill_id, name, raw=None):
    return {"canonical_skill_id": skill_id, "name": name, "raw": raw or name, "tier": "EXACT"}


# --- diff --------------------------------------------------------------------

def test_diff_lists_skills_and_core_changes():
    diff = build_parse_diff(
        before_skills=[_skill(PM, "Program Management"), _skill(None, None, "Programme Mgmt"), _skill("x", "Excel")],
        after_skills=[_skill(ALIAS, "ITBM"), _skill(AT, "AI Transformation"), _skill(None, None, "Agile Coaching")],
        before_evidence=[{"canonical_skill_id": PM}], after_evidence=[{"canonical_skill_id": PM}, {"canonical_skill_id": AT}],
        core=CORE, before_parsed={"skills": ["Program Management", "Excel"]},
        after_parsed={"skills": ["ITBM", "AI Transformation"]},
    )
    assert diff["skills_added"] == ["AI Transformation", "ITBM"]
    assert diff["skills_removed"] == ["Excel", "Program Management"]
    assert (diff["unknown_added"], diff["unknown_removed"]) == (["Agile Coaching"], ["Programme Mgmt"])
    assert diff["core_matched_before"] == ["Program Management"]
    assert diff["core_matched_after"] == ["Program Management", "AI Transformation"]   # alias still counts
    assert (diff["core_gained"], diff["core_lost"]) == (["AI Transformation"], [])
    assert diff["core_v2_evidence_after"] == ["Program Management", "AI Transformation"]
    assert diff["raw_skills_added"] == ["AI Transformation", "ITBM"] and diff["core_total"] == 3


# --- request rules -----------------------------------------------------------

def _service(stage=PipelineStage.REJECTED, parse_status=ParseStatus.PARSED):
    service = ResumeReparseService.__new__(ResumeReparseService)
    campaign = SimpleNamespace(id=uuid4(), jd_id=uuid4(), prompt_template_id=uuid4())
    resume = SimpleNamespace(id=uuid4(), parse_status=parse_status, parsed_json={"skills": ["Program Management"]})
    cc = SimpleNamespace(id=uuid4(), campaign_id=campaign.id, resume_id=resume.id, pipeline_stage=stage)
    service.db = MagicMock()
    service.db.execute.return_value.scalar_one.return_value = 1           # one earlier version
    service.db.execute.return_value.all.return_value = []
    service.campaign_candidate_repo = MagicMock(get_by_id=MagicMock(return_value=cc),
                                                get_by_resume_id=MagicMock(return_value=[cc]))
    service.campaign_repo = MagicMock(get_by_id=MagicMock(return_value=campaign))
    service.resume_repo = MagicMock(get_by_id=MagicMock(return_value=resume))
    service.audit_service = MagicMock()
    return service, cc, resume


def _request(service, cc, role, reason):
    with patch("app.tasks.resume_processing_tasks.process_resume_document") as task:
        result = service.request(campaign_candidate_id=cc.id, actor_id="u1", actor_role=role, reason=reason)
    return result, task


def test_recruiter_reparse_of_rejected_candidate_needs_a_reason():
    service, cc, _ = _service()
    with pytest.raises(CampaignException) as exc:
        _request(service, cc, "RECRUITER", "  ")
    assert exc.value.status_code == 422
    service.db.commit.assert_not_called()


def test_recruiter_reparse_of_rejected_candidate_moves_it_with_the_reason():
    service, cc, resume = _service()
    result, task = _request(service, cc, "RECRUITER", "Competency section was missed")
    assert result["moves_to_screening"] is True and result["stays_rejected"] is False
    kwargs = task.apply_async.call_args.kwargs["kwargs"]
    assert kwargs["reparse"]["move_to_screening"] is True
    assert kwargs["reparse"]["reason"] == "Competency section was missed"
    assert task.apply_async.call_args.kwargs["task_id"] == result["task_id"]
    service.resume_repo.mark_parse_pending.assert_called_once_with(resume)


def test_hr_admin_reparse_of_rejected_candidate_keeps_the_stage():
    service, cc, _ = _service()
    result, task = _request(service, cc, "HR_ADMIN", None)
    assert result["moves_to_screening"] is False and result["stays_rejected"] is True
    assert task.apply_async.call_args.kwargs["kwargs"]["reparse"]["move_to_screening"] is False


def test_previous_parse_is_kept_as_a_version():
    service, cc, resume = _service(stage=PipelineStage.SCREENING)
    result, _ = _request(service, cc, "RECRUITER", None)
    version = service.db.add.call_args.args[0]
    assert isinstance(version, ResumeParseVersion)
    assert version.parsed_json == resume.parsed_json and version.version_no == 2 == result["version_no"]


@pytest.mark.parametrize("status", [ParseStatus.PENDING, ParseStatus.PARSING, ParseStatus.FAILED])
def test_only_a_parsed_resume_can_be_reparsed(status):
    service, cc, _ = _service(parse_status=status)
    with pytest.raises(CampaignException) as exc:
        _request(service, cc, "RECRUITER", "x")
    assert exc.value.status_code == 409
    service.db.add.assert_not_called()


# --- persistence replaces skills ---------------------------------------------

def test_reparse_persistence_replaces_the_resume_skills():
    from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline
    pipeline = ResumeProcessingPipeline.__new__(ResumeProcessingPipeline)
    pipeline.resume_repository = MagicMock()
    pipeline.resume_service = MagicMock()
    pipeline.skill_repository = MagicMock()
    context = SimpleNamespace(resume_id=uuid4(), validated_extraction=MagicMock(), skill_match_results=[],
                              attempt_number=1, page_count=1, reparse=True)
    pipeline._run_persistence(context)
    assert pipeline.resume_service.persist_processed_resume.call_args.kwargs["replace_existing_skills"] is True


def test_persist_deletes_old_skills_first_only_on_reparse():
    from app.services.resume.resume_service import ResumeService
    for replace in (True, False):
        service = ResumeService.__new__(ResumeService)
        service.repository, service.audit_service = MagicMock(), MagicMock()
        resume = SimpleNamespace(id=uuid4(), candidate_id=uuid4(), uploaded_by="u1")
        service.persist_processed_resume(resume=resume, extraction=MagicMock(), skill_repository=MagicMock(),
                                         skill_matches=[], replace_existing_skills=replace)
        assert service.repository.delete_candidate_skills_by_resume.called is replace


# --- re-score path -------------------------------------------------------------

def test_rescore_moves_a_rejected_candidate_as_the_recruiter():
    service, cc, _ = _service()
    campaign = service.campaign_repo.get_by_id.return_value
    with patch("app.services.campaign.bulk_stage_move_service.BulkStageMoveService") as move:
        outcome = service._rescore(cc, campaign, {"move_to_screening": True, "reason": "missed section",
                                                  "actor_id": "u1", "actor_role": "RECRUITER"})
    kwargs = move.return_value.move_one.call_args.kwargs
    assert (kwargs["target_stage"], kwargs["actor_role"]) == ("SCREENING", "RECRUITER")
    assert "missed section" in kwargs["reason"] and outcome == "moved to SCREENING and re-scored"


def test_rescore_without_a_move_uses_the_manual_rescore():
    service, cc, _ = _service(stage=PipelineStage.SCREENING)
    with patch("app.services.campaign.manual_candidate_rescore.enqueue_manual_rescore", return_value=True) as rescore:
        outcome = service._rescore(cc, service.campaign_repo.get_by_id.return_value, {"move_to_screening": False})
    rescore.assert_called_once()
    assert outcome == "re-scored (screening)"


# --- replay after a simulated quota failure ----------------------------------

def test_quota_failure_dead_letters_with_the_reparse_mode_and_replays_as_a_reparse():
    from app.services.document_processing.error_classifier import FailureClassification
    from app.services.document_processing.retry_driver import RetryDriver
    from app.services.llm.base import LLMErrorReason, LLMPermanentError
    from app.tasks.resume_processing_tasks import replay_kwargs, replay_payload

    reparse = {"version_id": str(uuid4()), "campaign_candidate_id": str(uuid4()), "actor_id": "u1",
               "actor_role": "RECRUITER", "reason": "missed section", "move_to_screening": True}
    resume_id, prompt_id = uuid4(), uuid4()
    dlq = MagicMock()
    driver = RetryDriver(MagicMock(), MagicMock(), dlq, MagicMock(), SimpleNamespace(queued_at=None),
                         task_type="RESUME_DOCUMENT_PROCESSING")
    error = SimpleNamespace(original=LLMPermanentError("quota exceeded", reason=LLMErrorReason.DAILY_LIMIT),
                            stage=MagicMock())
    with patch("app.services.document_processing.retry_driver.classify",
               return_value=FailureClassification.PERMANENT):
        retried = driver.handle_failure(MagicMock(), "task-1", MagicMock(), error, 1, resume_id=resume_id,
                                        input_payload=replay_payload(resume_id, prompt_id, reparse))
    assert retried is False
    payload = dlq.create.call_args.kwargs["input_payload"]
    assert payload["reparse"] == reparse and payload["prompt_template_id"] == str(prompt_id)

    kwargs = replay_kwargs(SimpleNamespace(resume_id=resume_id, input_payload=payload))
    assert kwargs == {"resume_id": str(resume_id), "prompt_template_id": str(prompt_id), "reparse": reparse}


def test_campaign_replay_builder_keeps_reparse_mode_and_old_entries_still_replay():
    from app.services.campaign.campaign_service import CampaignService
    builder = CampaignService._DLQ_REPLAY_BUILDERS["RESUME_DOCUMENT_PROCESSING"]
    rid = uuid4()
    _, kwargs, linkable = builder(SimpleNamespace(resume_id=rid, input_payload={
        "resume_id": str(rid), "prompt_template_id": "p1", "reparse": {"version_id": "v"}}))
    assert linkable and kwargs["reparse"] == {"version_id": "v"} and kwargs["prompt_template_id"] == "p1"
    _, old_kwargs, _ = builder(SimpleNamespace(resume_id=rid, input_payload=None))
    assert old_kwargs == {"resume_id": str(rid)}          # prompt filled in by the replay loop from the campaign


def test_replace_mode_is_idempotent_for_catalogue_counters():
    from app.services.resume.resume_service import ResumeService
    from app.services.skills.skill_normalization_service import SkillMatchResult, SkillMatchTier
    old_id, new_id = uuid4(), uuid4()
    service = ResumeService.__new__(ResumeService)
    service.repository, service.audit_service = MagicMock(), MagicMock()
    service.repository.get_candidate_skills_by_resume.return_value = [
        SimpleNamespace(canonical_skill_id=old_id, raw_extracted_text="Program Management"),
        SimpleNamespace(canonical_skill_id=None, raw_extracted_text="Agile Coaching"),
    ]
    skill_repo = MagicMock()
    skill_repo.get_unknown_skill_by_raw_text.return_value = SimpleNamespace(id=uuid4())
    matches = [
        SkillMatchResult("Program Management", True, old_id, SkillMatchTier.EXACT, 1.0),
        SkillMatchResult("AI Governance", True, new_id, SkillMatchTier.EXACT, 1.0),
        SkillMatchResult("Agile Coaching", True, None, SkillMatchTier.UNKNOWN, None),
    ]
    resume = SimpleNamespace(id=uuid4(), candidate_id=uuid4(), uploaded_by="u1")
    service.persist_processed_resume(resume=resume, extraction=MagicMock(), skill_repository=skill_repo,
                                     skill_matches=matches, replace_existing_skills=True)
    skill_repo.bump_occurrence_counts.assert_called_once_with([new_id])   # only the newly matched skill
    skill_repo.upsert_unknown_skill.assert_not_called()                 # already-known unknown: no frequency bump
    service.repository.delete_candidate_skills_by_resume.assert_called_once_with(resume.id)


# --- re-score policy: never moves a stage past Screening ----------------------

@pytest.mark.parametrize("stage", [PipelineStage.SHORTLISTED, PipelineStage.INTERVIEW, PipelineStage.HM_REVIEW,
                                   PipelineStage.REJECTED])
def test_rescore_past_screening_is_score_only(stage):
    from app.services.resume import resume_reparse_service as module
    cc = SimpleNamespace(id=uuid4(), pipeline_stage=stage)
    with patch("app.services.campaign.manual_candidate_rescore.enqueue_manual_rescore") as full, \
            patch.object(module, "enqueue_score_only", return_value="scores updated") as score_only:
        assert module.request_rescore(MagicMock(), cc) == "scores updated"
    full.assert_not_called()
    score_only.assert_called_once()


def test_rescore_at_screening_runs_the_screening_flow():
    from app.services.resume import resume_reparse_service as module
    cc = SimpleNamespace(id=uuid4(), pipeline_stage=PipelineStage.SCREENING)
    with patch("app.services.campaign.manual_candidate_rescore.enqueue_manual_rescore", return_value=True), \
            patch.object(module, "enqueue_score_only") as score_only:
        assert module.request_rescore(MagicMock(), cc) == "re-scored (screening)"
    score_only.assert_not_called()


def test_score_only_enqueues_the_task_with_the_flag():
    from app.services.resume.resume_reparse_service import enqueue_score_only
    cc = SimpleNamespace(id=uuid4(), pipeline_stage=PipelineStage.SHORTLISTED)
    with patch("app.tasks.deterministic_scoring_tasks.calculate_deterministic_score_task") as task, \
            patch("app.services.celery_task_log_service.CeleryTaskLogService") as logs:
        logs.return_value.create_log.return_value = SimpleNamespace(task_id="t9")
        enqueue_score_only(MagicMock(), cc)
    assert task.apply_async.call_args.kwargs == {
        "kwargs": {"campaign_candidate_id": str(cc.id), "score_only": True}, "task_id": "t9"}


def test_hr_admin_reparse_of_rejected_candidate_rescores_score_only():
    service, cc, _ = _service()
    with patch("app.services.resume.resume_reparse_service.request_rescore", return_value="scores updated") as rescore:
        outcome = service._rescore(cc, service.campaign_repo.get_by_id.return_value, {"move_to_screening": False})
    rescore.assert_called_once()
    assert outcome == "scores updated" and cc.pipeline_stage == PipelineStage.REJECTED


# --- shared resume: other campaigns marked out of date, not re-scored ----------

def test_finish_marks_other_campaigns_out_of_date_without_rescoring_them():
    service, cc, resume = _service(stage=PipelineStage.SCREENING)
    other = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), score_out_of_date_at=None, score_out_of_date_reason=None,
                            pipeline_stage=PipelineStage.SHORTLISTED)
    service.campaign_candidate_repo.get_by_resume_id.return_value = [cc, other]
    service.campaign_repo.get_by_id.return_value.name = "AI transformation lead (test)"
    version = SimpleNamespace(skills_snapshot=[], evidence_snapshot=[], parsed_json={}, version_no=2, diff=None)
    service.db.get.return_value = version
    with patch.object(service, "_reembed"), \
            patch("app.services.resume.resume_reparse_service.request_rescore",
                  return_value="re-scored (screening)") as rescore:
        service.finish(resume_id=resume.id, reparse={"version_id": str(uuid4()), "campaign_candidate_id": str(cc.id),
                                                     "move_to_screening": False}, task_log_service=MagicMock())
    assert other.score_out_of_date_at is not None
    assert "AI transformation lead (test)" in other.score_out_of_date_reason
    rescore.assert_called_once_with(service.db, cc)            # only the requesting candidate
    assert version.diff["other_campaigns_marked_out_of_date"] == 1 and version.diff["status"] == "COMPLETED"
