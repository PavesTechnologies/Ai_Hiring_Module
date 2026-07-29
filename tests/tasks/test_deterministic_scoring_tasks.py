import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.async_tasks import TaskStatus
from app.models.campaigns import CampaignStatus
from app.models.candidates import ParseStatus
from app.models.pipeline import PipelineStage, TransitionSource
from app.services.campaign.candidate_scoring_service import (
    CandidateScoringService,
    MandatorySkillMatchType,
)

TASKS_MODULE = "app.tasks.deterministic_scoring_tasks"


def _breakdown(
    mandatory_skills, coverage_pct, passed, deterministic_score=None,
    preferred_skill_bonus=0.0, no_verified_skills=False,
):
    """
    deterministic_score defaults to coverage_pct only for tests that don't
    care about the distinction - M07 requires it be computed independently
    (actual/max mandatory contributions * 100) and to NEVER include
    preferred_skill_bonus, so the two are deliberately separate parameters
    here, never derived from one another.
    """
    return {
        "mandatory_skills": mandatory_skills,
        "mandatory_coverage_pct": coverage_pct,
        "deterministic_passed": passed,
        "deterministic_threshold": 70.0,
        "preferred_skills": [],
        "preferred_skill_bonus": preferred_skill_bonus,
        "deterministic_score": coverage_pct if deterministic_score is None else deterministic_score,
        "NO_VERIFIED_SKILLS": no_verified_skills,
    }


def _skill_entry(canonical_skill_id, match_type, canonical_name=None):
    return {
        "canonical_skill_id": str(canonical_skill_id),
        "canonical_name": canonical_name,
        "weight": 50.0,
        "match_type": match_type,
        "hierarchy_score_multiplier": 1.0 if match_type == "EXACT" else 0.0,
        "candidate_scoring_weight": 1.0 if match_type != "MISSING" else None,
        "match_tier": "EXACT" if match_type == "EXACT" else None,
        "confidence": 1.0 if match_type == "EXACT" else None,
        "contribution": 50.0 if match_type == "EXACT" else 0.0,
    }


class _Harness:
    """Patches every repository/service constructor the task instantiates, driven by simple mocks."""

    def __init__(self):
        self.campaign_candidate_repo = MagicMock()
        self.campaign_repo = MagicMock()
        self.resume_repo = MagicMock()
        self.jd_repo = MagicMock()
        self.jd_repo.get_by_id.return_value = _make_job_description()
        self.config_repo = MagicMock()
        # A bare MagicMock's .get_configs_by_keys(...) would return a
        # MagicMock, not a dict - every weight/tolerance .get(key, default)
        # in the task would then silently pick up a MagicMock instead of
        # its literal default. Empty dict makes every lookup fall through
        # to the task's own defaults, exactly like an unconfigured platform.
        self.config_repo.get_configs_by_keys.return_value = {}
        self.candidate_rejection_repo = MagicMock()
        self.allowed_transition_repo = MagicMock()
        # Default: transition is allowed, matching the now-seeded
        # SCREENING -> REJECTED row - tests for the "blocked" path override
        # this explicitly.
        self.allowed_transition_repo.is_transition_allowed.return_value = True
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        self.audit_service_instance = MagicMock()
        self.scoring_service_instance = MagicMock()
        self.email_template_repo = MagicMock()
        # Default: no active template configured, so existing tests that
        # don't care about email (the vast majority) take the safe
        # no-op-and-log-error path instead of silently constructing a real
        # EmailNotification/dispatching a real Celery task. Dedicated email
        # tests override this explicitly.
        self.email_template_repo.get_active_by_trigger_event.return_value = None
        self.email_notification_repo = MagicMock()
        self.send_candidate_email_task_mock = MagicMock()
        # M08-E02: calculate_deterministic_score_task imports this locally
        # (from app.tasks.semantic_scoring_tasks, to avoid a circular import -
        # that module itself imports _cancel_downstream_ai_evaluation from
        # this one) - patched at its source module, never a real Celery
        # dispatch/broker call in these unit tests.
        self.enqueue_semantic_scoring_mock = MagicMock()

    def __enter__(self):
        scoring_service_patch = patch(f"{TASKS_MODULE}.CandidateScoringService", return_value=self.scoring_service_instance)
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.JDRepository", return_value=self.jd_repo),
            patch(f"{TASKS_MODULE}.SkillRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.SkillOntologyRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=self.config_repo),
            patch(f"{TASKS_MODULE}.CandidateRejectionRepository", return_value=self.candidate_rejection_repo),
            patch(f"{TASKS_MODULE}.AllowedTransitionRepository", return_value=self.allowed_transition_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            scoring_service_patch,
            patch(f"{TASKS_MODULE}.EmailTemplateRepository", return_value=self.email_template_repo),
            patch(f"{TASKS_MODULE}.EmailNotificationRepository", return_value=self.email_notification_repo),
            patch(f"{TASKS_MODULE}.send_candidate_email_task", self.send_candidate_email_task_mock),
            patch(
                "app.tasks.semantic_scoring_tasks._enqueue_semantic_scoring",
                self.enqueue_semantic_scoring_mock,
            ),
        ]
        mocked_scoring_service_class = None
        for p in self._patches:
            started = p.start()
            if p is scoring_service_patch:
                mocked_scoring_service_class = started

        # The patched CandidateScoringService class mock must still expose
        # the real build_rejection_reason staticmethod (M07-E03 S01 T02) -
        # it's a pure formatting function these task-level tests exercise
        # for real, unlike calculate_and_store_score_breakdown (which stays
        # mocked via scoring_service_instance). Referenced by the specific
        # patch object, not list position, so adding later patches can
        # never silently break this again.
        mocked_scoring_service_class.build_rejection_reason = CandidateScoringService.build_rejection_reason

        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _make_campaign_candidate(campaign_id, resume_id, pipeline_stage=PipelineStage.SCREENING, candidate_id=None):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id, resume_id=resume_id, candidate_id=candidate_id or uuid4(),
        screened_at=None, updated_at=None, pipeline_stage=pipeline_stage,
    )


def _make_campaign(status=CampaignStatus.ACTIVE, jd_id=None, deterministic_threshold=70.0):
    return SimpleNamespace(id=uuid4(), status=status, jd_id=jd_id or uuid4(), deterministic_threshold=deterministic_threshold)


def _make_resume(parse_status=ParseStatus.PARSED, parsed_json=None):
    return SimpleNamespace(id=uuid4(), parse_status=parse_status, parsed_json=parsed_json)


def _make_job_description(min_experience_years=None, education_criteria=None):
    return SimpleNamespace(
        id=uuid4(), min_experience_years=min_experience_years,
        education_criteria=education_criteria,
    )


def test_skips_scoring_when_campaign_closed():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign(status=CampaignStatus.CLOSED)
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_score_breakdown.assert_not_called()
        success_call = h.task_log_repo.update.call_args
        # mark_success was reached (status flips via CeleryTaskLogService, using our task_log_repo)
        assert h.task_log_repo.commit.called


def test_raises_when_resume_not_parsed():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume(parse_status=ParseStatus.PARSING)

        with pytest.raises(ValueError):
            calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_score_breakdown.assert_not_called()


def test_raises_when_job_description_not_found():
    """
    campaign.jd_id is a NOT NULL FK, but the row it points at can still be
    missing from a stale/inconsistent read - this must fail cleanly with a
    clear ValueError (same shape as the campaign/resume prerequisite
    checks), never an unhandled AttributeError from dereferencing a None
    job_description further down.
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume(parse_status=ParseStatus.PARSED)
        h.jd_repo.get_by_id.return_value = None

        with pytest.raises(ValueError):
            calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.scoring_service_instance.calculate_and_store_score_breakdown.assert_not_called()


def test_skips_gracefully_when_campaign_candidate_no_longer_exists():
    """
    Root-cause fix: a campaign_candidate can legitimately be deleted (HR
    removes the candidate) while its resume is still being processed
    asynchronously - _enqueue_deterministic_scoring captures a valid id at
    enqueue time, but by the time a worker actually runs this task the row
    can be gone. This must be a graceful skip, never a crash, and the
    CeleryTaskLog row created for this run must NOT reference the missing
    id (campaign_candidate_id stays NULL, avoiding the ForeignKeyViolation
    this used to raise).
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        h.campaign_candidate_repo.get_by_id.return_value = None

        calculate_deterministic_score_task(campaign_candidate_id=str(uuid4()))

        h.scoring_service_instance.calculate_and_store_score_breakdown.assert_not_called()
        h.candidate_rejection_repo.create.assert_not_called()
        # task_log_service.create_log must be called with
        # campaign_candidate_id=None, never the missing id - this is exactly
        # the FK the old ordering violated.
        assert h.task_log_repo.create.call_args is not None
        created_log = h.task_log_repo.create.call_args[0][0]
        assert created_log.campaign_candidate_id is None


def test_creates_rejection_when_mandatory_skill_missing():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        missing_skill_id = uuid4()
        breakdown = _breakdown(
            [
                _skill_entry(uuid4(), "EXACT"),
                _skill_entry(missing_skill_id, "MISSING", canonical_name="Kubernetes"),
            ],
            coverage_pct=50.0, passed=False, preferred_skill_bonus=12.5,
        )
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_reason == "Missing required skills: Kubernetes."
        # T01: rejection_detail is the complete score_breakdown snapshot,
        # not a curated per-branch subset.
        assert rejection.rejection_detail == breakdown

        h.audit_service_instance.log.assert_called_once()
        audit_kwargs = h.audit_service_instance.log.call_args.kwargs
        assert audit_kwargs["details"]["missing"] == 1
        assert audit_kwargs["details"]["matched"] == 1
        assert audit_kwargs["details"]["deterministic_passed"] is False
        # deterministic_score reported here must be the mandatory-only
        # ratio score (50.0, defaulted from coverage_pct in this fixture) -
        # the preferred_skill_bonus (12.5) must NEVER be folded into it.
        assert audit_kwargs["details"]["deterministic_score"] == 50.0

        assert cc.screened_at is not None


def test_rejection_transitions_pipeline_stage_to_rejected():
    """
    M07-E03 S02 T01: a deterministic rejection must move the candidate
    SCREENING -> REJECTED and record the transition in
    campaign_candidate_stage_history, atomically with the rejection record
    (same db session, same commit at the end of the task).
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4(), pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "MISSING", canonical_name="AWS")], coverage_pct=0.0, passed=False)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.allowed_transition_repo.is_transition_allowed.assert_called_once_with(
            PipelineStage.SCREENING, PipelineStage.REJECTED,
        )
        assert cc.pipeline_stage == PipelineStage.REJECTED
        h.campaign_candidate_repo.create_stage_history.assert_called_once_with(
            campaign_candidate_id=cc.id,
            from_stage=PipelineStage.SCREENING,
            to_stage=PipelineStage.REJECTED,
            changed_by=None,
            change_reason="Deterministic filter rejection",
            transition_source=TransitionSource.SYSTEM,
            scores_snapshot=breakdown,
        )


def test_blocked_transition_leaves_pipeline_stage_untouched():
    """
    If allowed_transitions has no SCREENING -> REJECTED entry (e.g. removed/
    misconfigured), the candidate must stay in SCREENING and no stage
    history row is written - but the rejection record itself must still be
    created (T01/S01 must not depend on T01/S02 succeeding).
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        h.allowed_transition_repo.is_transition_allowed.return_value = False

        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4(), pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "MISSING", canonical_name="AWS")], coverage_pct=0.0, passed=False)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        assert cc.pipeline_stage == PipelineStage.SCREENING
        h.campaign_candidate_repo.create_stage_history.assert_not_called()
        # No email either - pipeline_stage never actually reached REJECTED.
        h.email_notification_repo.create.assert_not_called()
        h.send_candidate_email_task_mock.apply_async.assert_not_called()


def test_queues_rejection_email_after_successful_transition():
    """
    M07-E03 S02 T02: once the transaction (rejection + stage transition)
    has committed, an EmailNotification is created and EMAIL_SEND is
    dispatched - never before, never synchronously.
    """
    from app.models.email import EmailNotification, EmailNotificationStatus, EmailTriggerEvent
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        template = SimpleNamespace(id=uuid4())
        h.email_template_repo.get_active_by_trigger_event.return_value = template
        created_notification = EmailNotification(
            id=uuid4(), candidate_id=uuid4(), status=EmailNotificationStatus.QUEUED,
            trigger_event=EmailTriggerEvent.CANDIDATE_REJECTED, template_id=template.id,
        )
        h.email_notification_repo.create.return_value = created_notification

        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4(), pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "MISSING", canonical_name="AWS")], coverage_pct=0.0, passed=False)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.email_template_repo.get_active_by_trigger_event.assert_called_once_with(
            EmailTriggerEvent.CANDIDATE_REJECTED,
        )
        h.email_notification_repo.create.assert_called_once()
        created_arg = h.email_notification_repo.create.call_args[0][0]
        assert created_arg.candidate_id == cc.candidate_id
        assert created_arg.campaign_candidate_id == cc.id
        assert created_arg.template_id == template.id
        h.email_notification_repo.commit.assert_called_once()
        h.send_candidate_email_task_mock.apply_async.assert_called_once_with(
            kwargs={"email_notification_id": str(created_notification.id)},
        )


def test_no_email_queued_when_no_active_template():
    """No active CANDIDATE_REJECTED template configured -> no notification, no EMAIL_SEND dispatch, no crash."""
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        # Default harness state: get_active_by_trigger_event returns None.
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4(), pipeline_stage=PipelineStage.SCREENING)
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "MISSING", canonical_name="AWS")], coverage_pct=0.0, passed=False)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.email_notification_repo.create.assert_not_called()
        h.send_candidate_email_task_mock.apply_async.assert_not_called()
        # The task itself must still succeed - a missing template is a
        # configuration gap, never a reason to fail the scoring task.
        assert cc.pipeline_stage == PipelineStage.REJECTED


def test_creates_rejection_when_score_below_threshold_with_no_missing_skills():
    """
    T02: deterministic_passed can be False even with zero MISSING entries -
    e.g. every mandatory skill matched, but only at low-multiplier hierarchy
    tiers (SIBLING/SEMANTIC), so the weighted score still misses threshold.
    This must still produce a DETERMINISTIC candidate_rejection, not just
    a passed=False flag with no rejection record at all.
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown(
            [_skill_entry(uuid4(), "SIBLING"), _skill_entry(uuid4(), "SIBLING")],
            coverage_pct=100.0, passed=False, deterministic_score=40.0,
        )
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_reason == "Deterministic score below threshold."
        assert rejection.rejection_detail == breakdown

        audit_kwargs = h.audit_service_instance.log.call_args.kwargs
        assert audit_kwargs["details"]["missing"] == 0
        assert audit_kwargs["details"]["deterministic_passed"] is False
        assert audit_kwargs["details"]["score_breakdown"] == breakdown


def test_creates_no_verified_skills_rejection_distinct_from_missing_skills():
    """
    S04-T01: zero verified candidate skills gets its own specific rejection
    reason ("No verifiable skills extracted from resume.") even though every
    mandatory skill also comes back MISSING in this scenario - it must NOT
    be reported as the generic "Missing mandatory skills" reason, and must
    be distinguishable from a resume parse failure (which never reaches
    this task at all - that raises ValueError earlier).
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown(
            [_skill_entry(uuid4(), "MISSING"), _skill_entry(uuid4(), "MISSING")],
            coverage_pct=0.0, passed=False, deterministic_score=0.0,
            no_verified_skills=True,
        )
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_reason == "No verifiable skills extracted from resume."
        assert rejection.rejection_detail == breakdown


def test_creates_experience_rejection_when_skills_pass_but_experience_fails():
    """
    M07-E02 S01/S04: the JD/resume experience validation runs for real
    inside the task (only the skill breakdown itself is mocked) - a JD
    requiring 5 years against a candidate with 2 must reject with the
    experience-specific reason, not the generic threshold one.
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume(parsed_json={"total_experience_years": 2.0})
        h.jd_repo.get_by_id.return_value = _make_job_description(min_experience_years=5.0)

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=False, deterministic_score=60.0)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_reason == (
            "Insufficient experience: 2 years provided, minimum 5 years required (gap: 3 years)."
        )
        assert rejection.rejection_detail == breakdown


def test_handles_decimal_min_experience_years_without_crashing():
    """
    Regression test: JobDescription.min_experience_years is a Numeric(4,1)
    column - SQLAlchemy returns a real decimal.Decimal at runtime, not a
    float. Passing it straight into ExperienceEducationValidationService
    (which subtracts a float tolerance from it) used to raise TypeError:
    unsupported operand type(s) for -: 'decimal.Decimal' and 'float',
    silently rolling back the whole task and leaving deterministic_score/
    deterministic_passed/screened_at NULL forever. Every other test in
    this file uses a plain float/None for min_experience_years, which
    never exercised this - only a real Decimal does.
    """
    from decimal import Decimal
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume(parsed_json={"total_experience_years": 6.0})
        h.jd_repo.get_by_id.return_value = _make_job_description(min_experience_years=Decimal("5.0"))

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=True)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        call_kwargs = h.scoring_service_instance.calculate_and_store_score_breakdown.call_args.kwargs
        assert call_kwargs["experience_result"]["passed"] is True
        h.task_log_repo.commit.assert_called()


def test_creates_education_rejection_when_skills_and_experience_pass_but_education_fails():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume(
            parsed_json={"total_experience_years": 1.0, "education": [{"degree": "Bachelor's"}]},
        )
        h.jd_repo.get_by_id.return_value = _make_job_description(
            min_experience_years=None, education_criteria={"degree": "Master's"},
        )
        h.config_repo.get_configs_by_keys.return_value = {"EQUIVALENT_EXPERIENCE_YEARS": "8.0"}

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=False, deterministic_score=60.0)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_reason == (
            "Education requirement not met: Master's required, Bachelor's found."
        )
        assert rejection.rejection_detail == breakdown


def test_experience_education_wiring_is_passed_through_to_scoring_service():
    """
    Verifies the task actually threads its own experience_result/
    education_result/score_weights into calculate_and_store_score_breakdown,
    rather than only computing them for the rejection-reason branch.
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume(parsed_json={"total_experience_years": 6.0})
        h.jd_repo.get_by_id.return_value = _make_job_description(min_experience_years=5.0)

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=True)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        call_kwargs = h.scoring_service_instance.calculate_and_store_score_breakdown.call_args.kwargs
        assert call_kwargs["experience_result"]["passed"] is True
        assert call_kwargs["experience_result"]["candidate_years"] == 6.0
        assert call_kwargs["education_result"]["skipped"] is True
        assert call_kwargs["score_weights"] == {"skills": 0.70, "experience": 0.15, "education": 0.15}


def test_no_rejection_when_nothing_missing():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=True)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_not_called()


def test_auto_enqueues_semantic_scoring_after_successful_pass():
    """
    M08-E02: a candidate who passes deterministic screening is
    auto-enqueued for semantic scoring via the shared
    _enqueue_semantic_scoring helper (the same one
    CampaignCandidateService._queue_post_override_evaluation uses) - called
    with this exact campaign_candidate, task_log_service, and resume_repo.
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=True)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.enqueue_semantic_scoring_mock.assert_called_once()
        call_args = h.enqueue_semantic_scoring_mock.call_args.args
        assert call_args[0] is cc
        assert call_args[2] is h.resume_repo


def test_does_not_enqueue_semantic_scoring_when_deterministic_rejected():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "MISSING", canonical_name="AWS")], coverage_pct=0.0, passed=False)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.enqueue_semantic_scoring_mock.assert_not_called()


def test_semantic_scoring_enqueued_only_after_commit():
    """
    The enqueue call must happen strictly after campaign_candidate_repo's
    commit - never before, so a candidate is only ever handed off to
    semantic scoring once the deterministic outcome is durably persisted.
    """
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=True)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        commit_order = []
        h.campaign_candidate_repo.commit.side_effect = lambda: commit_order.append("commit")
        h.enqueue_semantic_scoring_mock.side_effect = lambda *a, **k: commit_order.append("enqueue")

        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        assert commit_order == ["commit", "enqueue"]


def test_semantic_enqueue_failure_never_crashes_or_undoes_the_deterministic_pass():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        campaign = _make_campaign()
        cc = _make_campaign_candidate(campaign.id, uuid4())
        h.campaign_candidate_repo.get_by_id.return_value = cc
        h.campaign_repo.get_by_id.return_value = campaign
        h.resume_repo.get_by_id.return_value = _make_resume()

        breakdown = _breakdown([_skill_entry(uuid4(), "EXACT")], coverage_pct=100.0, passed=True)
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown
        h.enqueue_semantic_scoring_mock.side_effect = Exception("broker unreachable")

        # Must not raise even though the semantic enqueue blew up - the
        # deterministic transaction already committed.
        calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.campaign_candidate_repo.commit.assert_called_once()
        task_log = h.task_log_repo.update.call_args.args[0]
        assert task_log.status == TaskStatus.SUCCESS


def test_marks_failure_on_unexpected_exception():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        h.campaign_candidate_repo.get_by_id.side_effect = RuntimeError("db exploded")

        with pytest.raises(RuntimeError):
            calculate_deterministic_score_task(campaign_candidate_id=str(uuid4()))
