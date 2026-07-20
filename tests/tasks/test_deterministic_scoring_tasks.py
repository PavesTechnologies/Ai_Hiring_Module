import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.models.campaigns import CampaignStatus
from app.models.candidates import ParseStatus
from app.services.campaign.candidate_scoring_service import MandatorySkillMatchType

TASKS_MODULE = "app.tasks.deterministic_scoring_tasks"


def _breakdown(mandatory_skills, coverage_pct, passed, preferred_bonus_score=0.0):
    return {
        "mandatory_skills": mandatory_skills,
        "mandatory_coverage_pct": coverage_pct,
        "deterministic_passed": passed,
        "deterministic_threshold": 70.0,
        "preferred_skills": [],
        "preferred_bonus_score": preferred_bonus_score,
        "deterministic_score": round(coverage_pct + preferred_bonus_score, 2),
    }


def _skill_entry(canonical_skill_id, match_type):
    return {
        "canonical_skill_id": str(canonical_skill_id),
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
        self.candidate_rejection_repo = MagicMock()
        self.task_log_repo = MagicMock()
        self.task_log_repo.get_by_task_id.return_value = None
        self.audit_service_instance = MagicMock()
        self.scoring_service_instance = MagicMock()

    def __enter__(self):
        self._patches = [
            patch(f"{TASKS_MODULE}.SessionLocal", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CampaignCandidateRepository", return_value=self.campaign_candidate_repo),
            patch(f"{TASKS_MODULE}.CampaignRepository", return_value=self.campaign_repo),
            patch(f"{TASKS_MODULE}.ResumeRepository", return_value=self.resume_repo),
            patch(f"{TASKS_MODULE}.SkillRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.SkillOntologyRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.ConfigRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CandidateRejectionRepository", return_value=self.candidate_rejection_repo),
            patch(f"{TASKS_MODULE}.AuditRepository", return_value=MagicMock()),
            patch(f"{TASKS_MODULE}.CeleryTaskLogRepository", return_value=self.task_log_repo),
            patch(f"{TASKS_MODULE}.AuditService", return_value=self.audit_service_instance),
            patch(f"{TASKS_MODULE}.CandidateScoringService", return_value=self.scoring_service_instance),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()


def _make_campaign_candidate(campaign_id, resume_id):
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id, resume_id=resume_id,
        screened_at=None, updated_at=None,
    )


def _make_campaign(status=CampaignStatus.ACTIVE, jd_id=None, deterministic_threshold=70.0):
    return SimpleNamespace(id=uuid4(), status=status, jd_id=jd_id or uuid4(), deterministic_threshold=deterministic_threshold)


def _make_resume(parse_status=ParseStatus.PARSED):
    return SimpleNamespace(id=uuid4(), parse_status=parse_status)


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


def test_raises_when_campaign_candidate_missing():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        h.campaign_candidate_repo.get_by_id.return_value = None

        with pytest.raises(ValueError):
            calculate_deterministic_score_task(campaign_candidate_id=str(uuid4()))


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
            [_skill_entry(uuid4(), "EXACT"), _skill_entry(missing_skill_id, "MISSING")],
            coverage_pct=50.0, passed=False, preferred_bonus_score=12.5,
        )
        h.scoring_service_instance.calculate_and_store_score_breakdown.return_value = breakdown

        fake_skill_ontology_repo = MagicMock()
        fake_skill_ontology_repo.get_skill_by_id.return_value = SimpleNamespace(canonical_name="Kubernetes")
        with patch(f"{TASKS_MODULE}.SkillOntologyRepository", return_value=fake_skill_ontology_repo):
            calculate_deterministic_score_task(campaign_candidate_id=str(cc.id))

        h.candidate_rejection_repo.create.assert_called_once()
        rejection = h.candidate_rejection_repo.create.call_args[0][0]
        assert rejection.rejection_reason == "Missing mandatory skills"
        assert rejection.rejection_detail == {"missing_skills": ["Kubernetes"]}

        h.audit_service_instance.log.assert_called_once()
        audit_kwargs = h.audit_service_instance.log.call_args.kwargs
        assert audit_kwargs["details"]["missing"] == 1
        assert audit_kwargs["details"]["matched"] == 1
        assert audit_kwargs["details"]["deterministic_passed"] is False
        # deterministic_score reported here must be the final blended score
        # (mandatory coverage + preferred bonus), not mandatory coverage alone.
        assert audit_kwargs["details"]["deterministic_score"] == 62.5

        assert cc.screened_at is not None


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


def test_marks_failure_on_unexpected_exception():
    from app.tasks.deterministic_scoring_tasks import calculate_deterministic_score_task

    with _Harness() as h:
        h.campaign_candidate_repo.get_by_id.side_effect = RuntimeError("db exploded")

        with pytest.raises(RuntimeError):
            calculate_deterministic_score_task(campaign_candidate_id=str(uuid4()))
