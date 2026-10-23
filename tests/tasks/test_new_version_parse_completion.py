"""
"Use new version": after process_resume_document parses the new version, the
campaign candidate that chose it is re-scored with request_rescore when it is
already past UPLOADED - left out of the first-parse path (UPLOADED ->
SCREENING, first deterministic scoring), so no stage moves. A candidate still
at UPLOADED (never scored) gets the normal first-parse path. A manual retry of
a failed new-version parse keeps the mode. Also: a re-score of an out-of-date
candidate in another campaign moves them onto the active version first.
Everything mocked - no LLM, storage, Celery or email.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.models.campaigns import CampaignStatus
from app.models.candidates import FileFormat, ParseStatus
from app.models.pipeline import PipelineStage

MODULE = "app.tasks.resume_processing_tasks"


def _run_task(resume, cc=None, **task_kwargs):
    pipeline = MagicMock()
    pipeline.run.return_value = resume.id
    resume_repo = MagicMock()
    resume_repo.get_by_id.return_value = resume
    task_log_repo = MagicMock()
    task_log_repo.get_by_task_id.return_value = SimpleNamespace(task_id="t", status=None)
    cc_repo = MagicMock()
    cc_repo.get_by_id.return_value = cc
    patches = {name: patch(f"{MODULE}.{name}", return_value=MagicMock()) for name in (
        "SessionLocal", "SkillRepository", "DocumentProcessingRepository", "AuditRepository", "StageExecutionService",
        "ResumeService", "EmbeddingService", "CheckpointRepository", "StageFailureLogRepository",
        "DeadLetterQueueRepository", "RetryDriver", "CacheService", "get_redis_client", "PreprocessingService",
        "LLMExtractionService", "resolve_active_provider", "StorageService", "SkillNormalizationService",
        "PIIDetectionService", "PIIRedactionService", "PromptTemplateRepository", "CeleryTaskLogService",
    )}
    patches["ResumeRepository"] = patch(f"{MODULE}.ResumeRepository", return_value=resume_repo)
    patches["CeleryTaskLogRepository"] = patch(f"{MODULE}.CeleryTaskLogRepository", return_value=task_log_repo)
    patches["CampaignCandidateRepository"] = patch(f"{MODULE}.CampaignCandidateRepository", return_value=cc_repo)
    patches["ResumeProcessingPipeline"] = patch(f"{MODULE}.ResumeProcessingPipeline", return_value=pipeline)
    patches["build_v2_detection"] = patch(f"{MODULE}.build_v2_detection", return_value=(MagicMock(), MagicMock()))
    patches["_v2_enabled_for_resume"] = patch(f"{MODULE}._v2_enabled_for_resume", return_value=False)
    for name in ("_move_parsed_candidates_to_screening", "_notify_resume_changed", "_enqueue_resume_embedding",
                 "_enqueue_deterministic_scoring", "_rescore_new_version"):
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


def _cc(resume, stage):
    return SimpleNamespace(id=uuid4(), resume_id=resume.id, campaign_id=uuid4(), pipeline_stage=stage)


def _excluded(mock):
    return mock.call_args.args[-1]


def test_new_version_past_uploaded_is_rescored_never_first_scored_or_moved():
    resume = _resume()
    cc = _cc(resume, PipelineStage.INTERVIEW)
    mocks = _run_task(resume, cc, new_version={"campaign_candidate_id": str(cc.id)})
    # The first-parse path still runs for any other candidate on the resume, never this one.
    assert _excluded(mocks["_move_parsed_candidates_to_screening"]) == frozenset({cc.id})
    assert _excluded(mocks["_enqueue_deterministic_scoring"]) == frozenset({cc.id})
    mocks["_rescore_new_version"].assert_called_once()
    assert mocks["_rescore_new_version"].call_args.args[1] is cc
    mocks["_enqueue_resume_embedding"].assert_called_once()
    assert cc.pipeline_stage == PipelineStage.INTERVIEW


def test_new_version_still_at_uploaded_gets_the_normal_first_scoring_path():
    resume = _resume()
    cc = _cc(resume, PipelineStage.UPLOADED)
    mocks = _run_task(resume, cc, new_version={"campaign_candidate_id": str(cc.id)})
    assert _excluded(mocks["_move_parsed_candidates_to_screening"]) == frozenset()
    assert _excluded(mocks["_enqueue_deterministic_scoring"]) == frozenset()
    mocks["_rescore_new_version"].assert_not_called()


def test_new_version_candidate_moved_off_the_resume_is_not_rescored():
    resume = _resume()
    cc = SimpleNamespace(id=uuid4(), resume_id=uuid4(), campaign_id=uuid4(), pipeline_stage=PipelineStage.SCREENING)
    mocks = _run_task(resume, cc, new_version={"campaign_candidate_id": str(cc.id)})
    assert _excluded(mocks["_enqueue_deterministic_scoring"]) == frozenset()
    mocks["_rescore_new_version"].assert_not_called()


def test_a_normal_parse_is_unchanged():
    mocks = _run_task(_resume())
    assert _excluded(mocks["_move_parsed_candidates_to_screening"]) == frozenset()
    assert _excluded(mocks["_enqueue_deterministic_scoring"]) == frozenset()
    mocks["_rescore_new_version"].assert_not_called()


def test_first_scoring_skips_the_excluded_candidate():
    from app.tasks.resume_processing_tasks import _enqueue_deterministic_scoring
    kept, excluded = SimpleNamespace(id=uuid4()), SimpleNamespace(id=uuid4())
    cc_repo = MagicMock()
    cc_repo.get_by_resume_id.return_value = [kept, excluded]
    log_service = MagicMock()
    log_service.repository.get_by_idempotency_key.return_value = None
    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=cc_repo), \
            patch(f"{MODULE}.calculate_deterministic_score_task") as task:
        _enqueue_deterministic_scoring(MagicMock(), uuid4(), log_service, frozenset({excluded.id}))
    assert [c.kwargs["kwargs"]["campaign_candidate_id"] for c in task.apply_async.call_args_list] == [str(kept.id)]


def test_stage_move_skips_the_excluded_candidate():
    from app.tasks.resume_processing_tasks import _move_parsed_candidates_to_screening
    excluded = SimpleNamespace(id=uuid4(), campaign_id=uuid4())
    cc_repo = MagicMock()
    cc_repo.get_by_resume_id.return_value = [excluded]
    with patch(f"{MODULE}.CampaignCandidateRepository", return_value=cc_repo), \
            patch(f"{MODULE}.CampaignRepository") as campaign_repo, \
            patch(f"{MODULE}.StageTransitionService") as transitions, \
            patch(f"{MODULE}.AllowedTransitionRepository"), patch(f"{MODULE}.AuditRepository"), \
            patch(f"{MODULE}.InterviewScheduleRepository"):
        _move_parsed_candidates_to_screening(MagicMock(), uuid4(), frozenset({excluded.id}))
    transitions.return_value.transition_to_screening.assert_not_called()
    campaign_repo.return_value.get_by_id.assert_not_called()


def _rescore(cc, campaign_status=CampaignStatus.ACTIVE):
    from app.tasks.resume_processing_tasks import _rescore_new_version
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = SimpleNamespace(status=campaign_status)
    with patch(f"{MODULE}.CampaignRepository", return_value=campaign_repo), \
            patch("app.services.resume.resume_reparse_service.request_rescore", return_value="rescored") as rescore:
        outcome = _rescore_new_version(MagicMock(), cc)
    return outcome, rescore


def test_rescore_uses_request_rescore():
    cc = _cc(_resume(), PipelineStage.SCREENING)
    outcome, rescore = _rescore(cc)
    assert outcome == "rescored" and rescore.call_args.args[1] is cc


def test_rescore_skips_a_closed_campaign():
    outcome, rescore = _rescore(_cc(_resume(), PipelineStage.SCREENING), CampaignStatus.CLOSED)
    assert outcome.startswith("skipped")
    rescore.assert_not_called()


# --- a manual retry keeps new-version mode ---------------------------------------------

def test_new_version_mode_is_read_from_the_parse_task_log_key():
    from app.tasks.resume_processing_tasks import new_version_idempotency_key, new_version_mode_for_resume
    resume_id, cc_id = uuid4(), uuid4()
    repo = MagicMock()
    repo.get_by_idempotency_key_prefix.return_value = SimpleNamespace(
        idempotency_key=new_version_idempotency_key(resume_id, cc_id))
    with patch(f"{MODULE}.CeleryTaskLogRepository", return_value=repo):
        assert new_version_mode_for_resume(MagicMock(), resume_id) == {"campaign_candidate_id": str(cc_id)}
    assert repo.get_by_idempotency_key_prefix.call_args.args[0] == (
        f"RESUME_DOCUMENT_PROCESSING:{resume_id}:new_version:")
    repo.get_by_idempotency_key_prefix.return_value = None
    with patch(f"{MODULE}.CeleryTaskLogRepository", return_value=repo):
        assert new_version_mode_for_resume(MagicMock(), resume_id) is None


def _retry(new_version):
    from app.services.resume.resume_upload_service import ResumeUploadService
    service = ResumeUploadService.__new__(ResumeUploadService)
    resume = SimpleNamespace(id=uuid4(), parse_status=ParseStatus.FAILED, task_id="old")
    service.resume_repo = MagicMock()
    service.resume_repo.get_by_id.return_value = resume
    service.audit_service = MagicMock()
    service.cache_service = None
    campaign = SimpleNamespace(id=uuid4(), prompt_template_id=uuid4())
    service._resolve_owning_campaign = MagicMock(return_value=campaign)
    target = "app.services.resume.resume_upload_service"
    with patch(f"{target}.new_version_mode_for_resume", return_value=new_version), \
            patch(f"{target}.process_resume_document") as task, patch(f"{target}.publish_task_reset"):
        service.retry_parse(resume.id, actor_id="rec-1", actor_role="RECRUITER")
    return task.apply_async.call_args.kwargs["kwargs"]


def test_retry_of_a_failed_new_version_parse_keeps_the_mode():
    mode = {"campaign_candidate_id": str(uuid4())}
    assert _retry(mode)["new_version"] == mode


def test_retry_of_a_normal_parse_is_unchanged():
    assert "new_version" not in _retry(None)


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
