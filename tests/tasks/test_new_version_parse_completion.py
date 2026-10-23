"""
"Use new version": after process_resume_document parses the new version, only
the campaign candidate that chose it is re-scored (request_rescore) - the
first-parse path (UPLOADED -> SCREENING, first deterministic scoring) never
runs for it, so no stage moves. Also: a re-score of an out-of-date candidate
in another campaign moves them onto the active version first. Everything
mocked - no LLM, storage, Celery or email.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.campaigns import CampaignStatus
from app.models.candidates import FileFormat, ParseStatus
from app.models.pipeline import PipelineStage

MODULE = "app.tasks.resume_processing_tasks"


def _run_task(resume, **task_kwargs):
    pipeline = MagicMock()
    pipeline.run.return_value = resume.id
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = resume
    task_log_repo = MagicMock()
    task_log_repo.get_by_task_id.return_value = SimpleNamespace(task_id="t", status=None)
    patches = {name: patch(f"{MODULE}.{name}", return_value=MagicMock()) for name in (
        "SessionLocal", "SkillRepository", "DocumentProcessingRepository", "AuditRepository", "StageExecutionService",
        "ResumeService", "EmbeddingService", "CheckpointRepository", "StageFailureLogRepository",
        "DeadLetterQueueRepository", "RetryDriver", "CacheService", "get_redis_client", "PreprocessingService",
        "LLMExtractionService", "resolve_active_provider", "StorageService", "SkillNormalizationService",
        "PIIDetectionService", "PIIRedactionService", "PromptTemplateRepository", "CeleryTaskLogService",
    )}
    patches["ResumeRepository"] = patch(f"{MODULE}.ResumeRepository", return_value=resume_repo)
    patches["CeleryTaskLogRepository"] = patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo)
    patches["ResumeProcessingPipeline"] = patch(f"{MODULE}.ResumeProcessingPipeline", return_value=pipeline)
    patches["build_v2_detection"] = patch(f"{MODULE}.build_v2_detection", return_value=(MagicMock(), MagicMock()))
    patches["_v2_enabled_for_resume"] = patch(f"{MODULE}._v2_enabled_for_resume", return_value=False)
    for name in ("_move_parsed_candidates_to_screening", "_notify_resume_changed", "_enqueue_resume_embedding",
                 "_enqueue_deterministic_scoring", "_finish_new_version"):
        patches[name] = patch(f"{MODULE}.{name}")
    started = {name: p.start() for name, p in patches.items()}
    try:
        from app.tasks.resume_processing_tasks import process_resume_document
        process_resume_document(resume_id=str(resume.id), prompt_template_id=str(uuid4()), **task_kwargs)
    finally:
        for p in patches.values():
            p.stop()
    return started


def _resume():
    return SimpleNamespace(id=uuid4(), candidate_id=uuid4(), file_format=FileFormat.PDF, file_path="p")


def test_new_version_parse_never_runs_the_first_parse_stage_move_or_scoring():
    resume = _resume()
    new_version = {"campaign_candidate_id": str(uuid4())}
    mocks = _run_task(resume, new_version=new_version)
    mocks["_move_parsed_candidates_to_screening"].assert_not_called()
    mocks["_enqueue_deterministic_scoring"].assert_not_called()
    mocks["_finish_new_version"].assert_called_once()
    assert mocks["_finish_new_version"].call_args.args[1:3] == (resume.id, new_version)
    mocks["_notify_resume_changed"].assert_called_once()


def test_a_normal_parse_is_unchanged():
    mocks = _run_task(_resume())
    mocks["_move_parsed_candidates_to_screening"].assert_called_once()
    mocks["_enqueue_deterministic_scoring"].assert_called_once()
    mocks["_finish_new_version"].assert_not_called()


def _finish(cc, campaign_status=CampaignStatus.ACTIVE):
    from app.tasks.resume_processing_tasks import _finish_new_version
    cc_repo = MagicMock()
    cc_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = SimpleNamespace(status=campaign_status)
    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=cc_repo), \
            patch(f"{MODULE}.CampaignRepository", return_value=campaign_repo), \
            patch(f"{MODULE}._enqueue_resume_embedding") as embed, \
            patch("app.services.resume.resume_reparse_service.request_rescore", return_value="rescored") as rescore:
        outcome = _finish_new_version(MagicMock(), cc.resume_id, {"campaign_candidate_id": str(cc.id)}, MagicMock())
    return outcome, rescore, embed


def test_finish_embeds_then_rescores_only_this_candidate_with_request_rescore():
    cc = SimpleNamespace(id=uuid4(), resume_id=uuid4(), campaign_id=uuid4(), pipeline_stage=PipelineStage.INTERVIEW)
    outcome, rescore, embed = _finish(cc)
    assert outcome == "rescored"
    embed.assert_called_once()
    rescore.assert_called_once()
    assert rescore.call_args.args[1] is cc
    assert cc.pipeline_stage == PipelineStage.INTERVIEW


def test_finish_skips_a_candidate_no_longer_on_the_new_version():
    cc = SimpleNamespace(id=uuid4(), resume_id=uuid4(), campaign_id=uuid4(), pipeline_stage=PipelineStage.SCREENING)
    from app.tasks.resume_processing_tasks import _finish_new_version
    cc_repo = MagicMock()
    cc_repo.get_by_id.return_value = cc
    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=cc_repo), \
            patch(f"{MODULE}._enqueue_resume_embedding"), \
            patch("app.services.resume.resume_reparse_service.request_rescore") as rescore:
        outcome = _finish_new_version(MagicMock(), uuid4(), {"campaign_candidate_id": str(cc.id)}, MagicMock())
    assert outcome.startswith("skipped")
    rescore.assert_not_called()


def test_finish_skips_a_closed_campaign():
    cc = SimpleNamespace(id=uuid4(), resume_id=uuid4(), campaign_id=uuid4(), pipeline_stage=PipelineStage.SCREENING)
    outcome, rescore, _ = _finish(cc, CampaignStatus.CLOSED)
    assert outcome.startswith("skipped")
    rescore.assert_not_called()


# --- other campaigns re-score onto the active version ------------------------------

def _reparse_service(cc, active, current):
    from app.services.resume.resume_reparse_service import ResumeReparseService
    service = ResumeReparseService.__new__(ResumeReparseService)
    service.db = MagicMock()
    service.campaign_candidate_repo = MagicMock(get_by_id=MagicMock(return_value=cc))
    service.resume_repo = MagicMock()
    service.resume_repo.get_active_by_candidate.return_value = active
    service.resume_repo.get_by_id.return_value = current
    service.audit_service = MagicMock()
    return service


def test_rescore_out_of_date_repoints_to_the_parsed_active_version():
    old = SimpleNamespace(id=uuid4(), version_number=1, parse_status=ParseStatus.PARSED)
    active = SimpleNamespace(id=uuid4(), version_number=2, parse_status=ParseStatus.PARSED)
    cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=old.id,
                         pipeline_stage=PipelineStage.SHORTLISTED, score_out_of_date_reason="new version")
    service = _reparse_service(cc, active, old)
    with patch("app.services.resume.resume_reparse_service.request_rescore", return_value="scores updated") as rescore:
        result = service.rescore_out_of_date(campaign_candidate_id=cc.id, actor_id="rec-1", actor_role="RECRUITER")
    assert cc.resume_id == active.id and result["repointed_to_active_version"] is True
    details = service.audit_service.log.call_args.kwargs["details"]
    assert details["repointed_to_active_version"] == {
        "from_resume_id": str(old.id), "from_version_number": 1, "to_resume_id": str(active.id),
        "to_version_number": 2,
    }
    rescore.assert_called_once_with(service.db, cc)
    assert cc.pipeline_stage == PipelineStage.SHORTLISTED


def test_rescore_out_of_date_keeps_the_resume_while_the_active_version_is_not_parsed():
    old = SimpleNamespace(id=uuid4(), version_number=1, parse_status=ParseStatus.PARSED)
    active = SimpleNamespace(id=uuid4(), version_number=2, parse_status=ParseStatus.PARSING)
    cc = SimpleNamespace(id=uuid4(), campaign_id=uuid4(), candidate_id=uuid4(), resume_id=old.id,
                         pipeline_stage=PipelineStage.SCREENING, score_out_of_date_reason=None)
    service = _reparse_service(cc, active, old)
    with patch("app.services.resume.resume_reparse_service.request_rescore", return_value="ok"):
        result = service.rescore_out_of_date(campaign_candidate_id=cc.id, actor_id="rec-1", actor_role="RECRUITER")
    assert cc.resume_id == old.id and result["repointed_to_active_version"] is False
    assert "repointed_to_active_version" not in service.audit_service.log.call_args.kwargs["details"]
