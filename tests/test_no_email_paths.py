"""
Standing rule: no scoring v2, re-parse, re-score or shadow code path may send email.

Two layers:
  1. Static - none of those modules imports an email module or calls a send
     function (catches a future edit that wires one in).
  2. Runtime - every email entry point (SES client, boto3 SES, the email
     Celery task, the notification queue_* helpers) is replaced with one that
     fails the test, then each path is run.
Also: the sender itself is log-only in tests (EMAIL_DELIVERY=log, conftest).
"""
import ast
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]

V2_MODULES = [
    "app/services/campaign/scoring_v2.py",
    "app/services/campaign/scoring_v2_service.py",
    "app/services/campaign/scoring_shadow_report_service.py",
    "app/tasks/scoring_v2_tasks.py",
    "app/services/resume/resume_reparse_service.py",
    "app/services/skills/core_skill_confirmation_service.py",
    "app/services/skills/skill_evidence_service.py",
    "app/services/skills/resume_skill_scanner.py",
    "app/services/skills/skill_sense_resolver.py",
    "app/services/skills/resume_sections.py",
    "app/services/llm/structured_call.py",
    "app/repositories/scoring_result_repository.py",
    "app/repositories/skill_detection_repository.py",
    "app/seeds/seed_scoring_v2.py",
    "scripts/skill_detection_live_runs.py",
    "scripts/scoring_v2_dry_run.py",
    "scripts/e2e/export_fixtures.py",
    "scripts/e2e/run_reparse_e2e.py",
    "scripts/e2e/setup_e2e.py",
    "scripts/e2e/stub_llm.py",
    "scripts/e2e/worker.py",
    "scripts/scoring_v2_shadow_run.py",
    # Duplicate-file upload fix (separate change) - also email-free.
    "app/services/resume/resume_intake_service.py",
    "app/services/resume/resume_upload_service.py",
]
EMAIL_MODULE_MARKERS = ("ses_email_client", "email_tasks", "candidate_notification_emails",
                        "candidate_rejection_email_service", "interview_feedback_request_emails",
                        "interview_interviewer_lifecycle_emails")
EMAIL_CALL_MARKERS = ("send_email", "send_candidate_email", "queue_rejection_email")


@pytest.mark.parametrize("path", V2_MODULES)
def test_v2_modules_never_import_or_call_email(path):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [node.module or ""] if isinstance(node, ast.ImportFrom) else [a.name for a in node.names]
            names += [a.name for a in node.names]
            assert not any(m in n for n in names for m in EMAIL_MODULE_MARKERS), f"{path} imports email: {names}"
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            assert not (name and (name in EMAIL_CALL_MARKERS or (name.startswith("queue_") and "email" in name))), \
                f"{path} calls {name}"


class EmailAttempted(AssertionError):
    pass


def _fail(*args, **kwargs):
    raise EmailAttempted("an email entry point was called")


@pytest.fixture
def email_guard():
    """Every way the app can send email fails the test if touched."""
    import boto3
    real_client = boto3.client

    def guarded_client(service, *args, **kwargs):
        if service == "ses":
            raise EmailAttempted("an SES client was created")
        return real_client(service, *args, **kwargs)

    patches = [
        patch("app.services.notifications.ses_email_client.SESEmailClient.send_email", _fail),
        patch("boto3.client", guarded_client),
    ]
    email_tasks = importlib.import_module("app.tasks.email_tasks")
    patches += [patch.object(email_tasks.send_candidate_email_task, "apply_async", _fail),
                patch.object(email_tasks.send_candidate_email_task, "delay", _fail)]
    for module_name in ("candidate_notification_emails", "candidate_rejection_email_service",
                        "interview_feedback_request_emails", "interview_interviewer_lifecycle_emails"):
        module = importlib.import_module(f"app.services.notifications.{module_name}")
        for attr in dir(module):
            if attr.startswith("queue_") and callable(getattr(module, attr)):
                patches.append(patch.object(module, attr, _fail))
    rejection = importlib.import_module("app.services.notifications.candidate_rejection_email_service")
    patches.append(patch.object(rejection.CandidateRejectionEmailService, "queue_rejection_email", _fail))
    for p in patches:
        p.start()
    yield
    for p in reversed(patches):
        p.stop()


def test_guard_really_catches_a_send(email_guard):
    from app.services.notifications.ses_email_client import SESEmailClient
    with pytest.raises(EmailAttempted):
        SESEmailClient().send_email(to_address="x@example.invalid", subject="s", body_text="b")


def test_sender_is_log_only_in_tests():
    """The sender level, not just skipped calls: no SES client is created in log mode."""
    from app.services.notifications.ses_email_client import SESEmailClient
    with patch("boto3.client", side_effect=EmailAttempted("SES client created")):
        message_id = SESEmailClient().send_email(to_address="x@example.invalid", subject="s", body_text="b")
    assert message_id.startswith("log-only-")


# --- runtime: each v2 / re-parse / shadow path under the guard ---------------

def _reparse_service(stage):
    from app.models.candidates import ParseStatus
    from app.services.resume.resume_reparse_service import ResumeReparseService
    service = ResumeReparseService.__new__(ResumeReparseService)
    campaign = SimpleNamespace(id=uuid4(), jd_id=uuid4(), prompt_template_id=uuid4(), name="t")
    resume = SimpleNamespace(id=uuid4(), parse_status=ParseStatus.PARSED, parsed_json={})
    cc = SimpleNamespace(id=uuid4(), campaign_id=campaign.id, resume_id=resume.id, pipeline_stage=stage,
                         score_out_of_date_reason=None)
    service.db = MagicMock()
    service.db.execute.return_value.scalar_one.return_value = 0
    service.db.execute.return_value.all.return_value = []
    service.campaign_candidate_repo = MagicMock(get_by_id=MagicMock(return_value=cc),
                                                get_by_resume_id=MagicMock(return_value=[cc]))
    service.campaign_repo = MagicMock(get_by_id=MagicMock(return_value=campaign))
    service.resume_repo = MagicMock(get_by_id=MagicMock(return_value=resume))
    service.audit_service = MagicMock()
    return service, cc, campaign


def test_reparse_request_finish_and_rescore_send_no_email(email_guard):
    from app.models.pipeline import PipelineStage
    service, cc, campaign = _reparse_service(PipelineStage.REJECTED)
    with patch("app.tasks.resume_processing_tasks.process_resume_document"):
        service.request(campaign_candidate_id=cc.id, actor_id="u1", actor_role="RECRUITER", reason="reconsider this")
    version = SimpleNamespace(skills_snapshot=[], evidence_snapshot=[], parsed_json={}, version_no=1, diff=None)
    service.db.get.return_value = version
    with patch("app.services.campaign.pipeline_transition_service.PipelineTransitionService"), \
            patch("app.services.campaign.manual_candidate_rescore.enqueue_manual_rescore", return_value=True):
        service.finish(resume_id=cc.resume_id, reparse={"version_id": str(uuid4()), "campaign_candidate_id": str(cc.id),
                                                        "move_to_screening": True, "reason": "reconsider this",
                                                        "actor_id": "u1", "actor_role": "RECRUITER"},
                       task_log_service=MagicMock())
    assert version.diff["status"] == "COMPLETED"


@pytest.mark.parametrize("stage_name", ["SHORTLISTED", "SCREENING", "REJECTED"])
def test_rescore_paths_send_no_email(email_guard, stage_name):
    from app.models.pipeline import PipelineStage
    from app.services.resume.resume_reparse_service import request_rescore
    service, cc, _ = _reparse_service(PipelineStage[stage_name])
    with patch("app.services.campaign.manual_candidate_rescore.enqueue_manual_rescore", return_value=True):
        request_rescore(MagicMock(), cc)
    service.rescore_out_of_date(campaign_candidate_id=cc.id, actor_id="u1", actor_role="RECRUITER")


def test_core_confirmation_sends_no_email(email_guard):
    from app.services.skills.core_skill_confirmation_service import CoreSkillConfirmationService, CoreSkillSpec
    skill = uuid4()
    provider = MagicMock(provider_name="GOOGLE", model="m", last_model_used=None, last_used_fallback=False)
    provider.generate_json.return_value = {"items": [{"number": 1, "present": False, "quote": ""}]}
    CoreSkillConfirmationService(provider).confirm(
        resume_id=uuid4(), jd_id=uuid4(), core_skills=[CoreSkillSpec(skill, "AI Transformation")],
        evidence_rows=[{"canonical_skill_id": skill, "sense": None, "sources": ["DERIVED"],
                        "evidence": [{"text": "AI & Digital Transformation", "source": "DERIVED", "section": "SKILLS"}]}],
        text_hash="h")


@pytest.mark.parametrize("outcome", ["PENDING", "REJECT", "PASS"])
def test_shadow_task_sends_no_email(email_guard, outcome):
    import app.tasks.scoring_v2_tasks as tasks
    task = tasks.calculate_scoring_v2_task
    campaign = SimpleNamespace(id=uuid4(), jd_id=uuid4(), scoring_v2_enabled=False, scoring_v2_shadow=True)
    cc = SimpleNamespace(id=uuid4(), campaign_id=campaign.id, resume_id=uuid4())
    service = MagicMock()
    service.return_value.score.return_value = {"outcome": outcome, "confirmation": {}}
    with patch.object(tasks, "SessionLocal", return_value=MagicMock()), \
            patch.object(tasks, "CeleryTaskLogService") as logs, \
            patch.object(tasks, "CampaignCandidateRepository") as cc_repo, \
            patch.object(tasks, "CampaignRepository") as campaign_repo, \
            patch.object(tasks, "ResumeRepository"), patch.object(tasks, "JDRepository"), \
            patch.object(tasks, "ConfigRepository"), patch.object(tasks, "SkillRepository"), \
            patch.object(tasks, "ScoringV2Service", service), patch.object(tasks, "DeadLetterQueueRepository"), \
            patch("app.tasks.deterministic_scoring_tasks.build_validation_inputs", return_value=({}, {}, {}, {})), \
            patch.object(task, "retry", side_effect=RuntimeError("retry")):
        logs.return_value.repository.get_by_task_id.return_value = None
        cc_repo.return_value.get_by_id.return_value = cc
        campaign_repo.return_value.get_by_id.return_value = campaign
        for retries in (0, 3):                       # first attempt and the dead-letter path
            task.push_request(id=f"t{retries}", retries=retries)
            try:
                task.run(campaign_candidate_id=str(cc.id))
            except RuntimeError:
                pass
            finally:
                task.pop_request()


def test_shadow_switch_and_backfill_send_no_email(email_guard):
    from app.services.campaign.scoring_shadow_report_service import ScoringShadowReportService
    service = ScoringShadowReportService.__new__(ScoringShadowReportService)
    service.db = MagicMock()
    service.db.execute.return_value.scalars.return_value.all.return_value = [SimpleNamespace(id=uuid4())]
    service.campaign_repo = MagicMock(get_by_id=MagicMock(return_value=SimpleNamespace(id=uuid4(), scoring_v2_shadow=False)))
    service.audit_service = MagicMock()
    with patch("app.tasks.scoring_v2_tasks.enqueue_scoring_v2", return_value=True):
        result = service.set_shadow(uuid4(), enabled=True, backfill=True, actor_id=None, actor_role="SYSTEM")
    assert result["queued"] == 1
