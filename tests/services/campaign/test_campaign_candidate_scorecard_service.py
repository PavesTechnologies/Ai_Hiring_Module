from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import DecisionSource, DecisionType, PipelineStage
from app.schemas.campaign.campaign_candidate_schema import (
    CandidateRejectionHistoryEntryResponse,
    CandidateScorecardResponse,
)
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
M07-E03 S03 - Display Rejection Reason on Candidate Scorecard.

T01: get_campaign_candidate_scorecard / _build_rejection_banner
T02: get_rejection_history
T03: export_rejected_candidates / _to_export_row / gap-display helpers
"""


def _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, score_breakdown=None, deterministic_score=42.5):
    # Model fields since the decision_* redesign (98d721e): the breakdown is
    # deterministic_breakdown and the current decision lives in decision_*.
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        deterministic_breakdown=score_breakdown or {},
        semantic_breakdown=None, semantic_passed=None, previous_stage=None,
        decision_type=None, decision_source=None, decision_reason=None, decision_details=None,
        decision_by_user_id=None, decision_at=None,
        ai_evaluation=None,
        deterministic_score=deterministic_score,
        semantic_score=None,
        composite_score=None,
        created_at=datetime.now(timezone.utc),
    )


def _reject(candidate, source=DecisionSource.DETERMINISTIC, reason="Missing required skills: Python."):
    """Record a rejection on the candidate, as StageTransitionService.transition_to_rejected does."""
    candidate.pipeline_stage = PipelineStage.REJECTED
    candidate.decision_type = DecisionType.REJECTED
    candidate.decision_source = source
    candidate.decision_reason = reason
    candidate.decision_at = datetime.now(timezone.utc)
    return candidate


def _override(candidate, to_stage, override_reason="HR approved"):
    """An HR override (RESET): moves the candidate off REJECTED and keeps the original rejection in decision_details."""
    original_source, original_reason, original_at = candidate.decision_source, candidate.decision_reason, candidate.decision_at
    candidate.pipeline_stage = to_stage
    candidate.decision_type = DecisionType.RESET
    candidate.decision_source = DecisionSource.RECRUITER
    candidate.decision_reason = override_reason
    candidate.decision_at = datetime.now(timezone.utc)
    candidate.decision_details = {
        "overridden_decision_type": DecisionType.REJECTED.value,
        "overridden_decision_source": original_source.value,
        "overridden_decision_reason": original_reason,
        "overridden_decision_at": original_at.isoformat(),
    }
    return original_reason, original_at


def _rejection_row(reason, source=DecisionSource.DETERMINISTIC):
    """A to_stage=REJECTED stage-history row (the rejection record since candidate_rejections was removed)."""
    return SimpleNamespace(id=uuid4(), to_stage=PipelineStage.REJECTED, changed_at=datetime.now(timezone.utc),
                           scores_snapshot={"decision_source": source.value, "decision_reason": reason})


def make_service(campaign_candidate_repo=None, candidate_repo=None, resume_repo=None, campaign_repo=None,
                 audit_service=None):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
        encryption_service=MagicMock(),
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
    )


def _repo_returning(candidate):
    repo = MagicMock()
    repo.get_by_id.return_value = candidate
    return repo


# ----------------------------------------------------------------------
# T01 - Show Rejection Banner & Reason
# ----------------------------------------------------------------------

def test_scorecard_returns_not_found_when_candidate_missing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_campaign_candidate_scorecard(uuid4())

    assert exc_info.value.status_code == 404


def test_scorecard_banner_present_for_deterministic_rejection():
    candidate = _reject(_make_campaign_candidate())
    service = make_service(
        campaign_candidate_repo=_repo_returning(candidate),
        candidate_repo=MagicMock(get_by_id=MagicMock(return_value=None)),
        resume_repo=MagicMock(get_by_id=MagicMock(return_value=None)),
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert isinstance(scorecard, CandidateScorecardResponse)
    assert scorecard.has_rejection is True
    assert scorecard.rejection_layer == DecisionSource.DETERMINISTIC
    assert scorecard.rejection_reason == candidate.decision_reason
    assert scorecard.rejected_at == candidate.decision_at
    assert scorecard.is_overridden is False
    assert scorecard.status is None


def test_scorecard_banner_absent_when_rejection_layer_is_not_deterministic():
    candidate = _reject(_make_campaign_candidate(), source=DecisionSource.SEMANTIC, reason="Low semantic similarity.")
    service = make_service(campaign_candidate_repo=_repo_returning(candidate))

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.has_rejection is False
    assert scorecard.rejection_layer is None
    assert scorecard.rejection_reason is None


def test_scorecard_banner_absent_when_pipeline_stage_is_not_rejected():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    service = make_service(campaign_candidate_repo=_repo_returning(candidate))

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.has_rejection is False
    assert scorecard.rejection_reason is None


def test_scorecard_reports_overridden_status_while_preserving_original_reason():
    candidate = _reject(_make_campaign_candidate(),
                        reason="Insufficient experience: 2 years provided, minimum 4 years required (gap: 2 years).")
    original_reason, original_at = _override(candidate, to_stage=PipelineStage.UPLOADED)
    service = make_service(campaign_candidate_repo=_repo_returning(candidate))

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.is_overridden is True
    assert scorecard.status == "Overridden — Previously Rejected"
    # Original rejection reason/timestamp must be preserved, never rewritten by the override.
    assert scorecard.rejection_reason == original_reason
    assert scorecard.rejected_at == original_at


def test_scorecard_preserves_rejection_reason_after_a_real_override_moves_stage_off_rejected():
    """
    An applied override moves pipeline_stage off REJECTED: has_rejection reflects
    "currently REJECTED" (False), but is_overridden/status/rejection_reason/
    rejected_at/rejection_layer still describe the original deterministic rejection.
    """
    candidate = _reject(_make_campaign_candidate(), reason="Missing required skills: AWS.")
    original_reason, original_at = _override(candidate, to_stage=PipelineStage.SCREENING)
    service = make_service(campaign_candidate_repo=_repo_returning(candidate))

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.has_rejection is False  # not currently in the REJECTED stage
    assert scorecard.is_overridden is True
    assert scorecard.status == "Overridden — Previously Rejected"
    assert scorecard.rejection_reason == original_reason
    assert scorecard.rejected_at == original_at
    assert scorecard.rejection_layer == DecisionSource.DETERMINISTIC


def test_scorecard_score_breakdown_read_from_campaign_candidate_not_rejection_snapshot():
    breakdown = {"deterministic_score": 40.0, "deterministic_passed": False}
    candidate = _reject(_make_campaign_candidate(score_breakdown=breakdown))
    service = make_service(campaign_candidate_repo=_repo_returning(candidate))

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.score_breakdown == breakdown


# ----------------------------------------------------------------------
# T02 - Show Rejection History (read from REJECTED stage-history rows)
# ----------------------------------------------------------------------

def test_rejection_history_not_found_when_candidate_missing():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_rejection_history(uuid4())

    assert exc_info.value.status_code == 404


def test_rejection_history_orders_newest_first_with_correct_rounds():
    candidate = _make_campaign_candidate()
    oldest = _rejection_row("Missing required skills: Python.")
    newest = _rejection_row("Missing required skills: AWS.")
    campaign_candidate_repo = _repo_returning(candidate)
    # Stage history comes back oldest-first; non-REJECTED rows are ignored.
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = [
        oldest, SimpleNamespace(id=uuid4(), to_stage=PipelineStage.SCREENING, changed_at=None, scores_snapshot={}),
        newest,
    ]
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    history = service.get_rejection_history(candidate.id)

    assert len(history) == 2
    assert all(isinstance(entry, CandidateRejectionHistoryEntryResponse) for entry in history)
    assert history[0].id == newest.id
    assert history[0].current_status is True
    assert history[0].evaluation_round == 2
    assert history[0].rejection_reason == "Missing required skills: AWS."
    assert history[1].id == oldest.id
    assert history[1].current_status is False
    assert history[1].evaluation_round == 1


def test_rejection_history_is_empty_when_never_rejected():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    campaign_candidate_repo = _repo_returning(candidate)
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = [
        SimpleNamespace(id=uuid4(), to_stage=PipelineStage.SCREENING, changed_at=None, scores_snapshot={}),
    ]
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    assert service.get_rejection_history(candidate.id) == []


# ----------------------------------------------------------------------
# T03 - Export Rejected Candidate Details
# ----------------------------------------------------------------------

def test_export_rejected_candidates_not_found_when_campaign_missing():
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = None
    service = make_service(campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.export_rejected_candidates(uuid4(), actor_id="user-1", actor_role="HR_ADMIN")

    assert exc_info.value.status_code == 404


def test_export_rejected_candidates_writes_rows_and_logs_audit(monkeypatch):
    campaign = SimpleNamespace(id=uuid4())
    breakdown = {
        "mandatory_skills": [
            {"canonical_name": "Python", "match_type": "MISSING"},
            {"canonical_name": "AWS", "match_type": "EXACT"},
        ],
        "experience_validation": {"passed": False, "candidate_years": 2.5, "min_years": 4},
        "education_validation": {"passed": True},
    }
    candidate_1 = _reject(_make_campaign_candidate(score_breakdown=breakdown), reason="Missing required skills: Python.")

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_rejected_by_campaign.return_value = [candidate_1]
    audit_service = MagicMock()

    captured_rows = {}

    def fake_export_rejected_candidates(rows):
        captured_rows["rows"] = rows
        import io
        return io.BytesIO(b"fake-xlsx-bytes")

    monkeypatch.setattr(
        "app.services.campaign.campaign_candidate_service.ExcelExport.export_rejected_candidates",
        fake_export_rejected_candidates,
    )

    service = make_service(campaign_repo=campaign_repo, campaign_candidate_repo=campaign_candidate_repo,
                           audit_service=audit_service)

    response = service.export_rejected_candidates(campaign.id, actor_id="user-1", actor_role="HR_ADMIN")

    rows = captured_rows["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_uuid"] == str(candidate_1.candidate_id)
    assert row["rejection_layer"] == DecisionSource.DETERMINISTIC.value
    assert row["rejection_reason"] == "Missing required skills: Python."
    assert row["rejected_at"] == candidate_1.decision_at
    assert row["missing_mandatory_skills"] == "Python"
    assert "2.5 years provided, 4 years required" in row["experience_gap"]
    assert row["education_gap"] == ""
    # A rejected candidate's current decision is the rejection, so no override.
    assert row["hr_override"] is False
    assert row["override_reason"] == ""

    # Never exports PII.
    for forbidden in ("candidate_name", "email", "phone", "resume"):
        assert forbidden not in row

    audit_service.log.assert_called_once()
    _, kwargs = audit_service.log.call_args
    assert kwargs["action_type"].value == "REJECTED_CANDIDATES_EXPORTED"
    assert kwargs["details"] == {"exported_count": 1}

    assert response.media_type == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert "attachment; filename=" in response.headers["content-disposition"]


def test_missing_mandatory_skills_display_lists_only_missing_skills():
    breakdown = {
        "mandatory_skills": [
            {"canonical_name": "Python", "match_type": "MISSING"},
            {"canonical_name": "AWS", "match_type": "EXACT"},
            {"canonical_name": "Docker", "match_type": "MISSING"},
        ]
    }
    result = CampaignCandidateService._missing_mandatory_skills_display(breakdown)
    assert result == "Python, Docker"


def test_experience_gap_display_blank_when_passed():
    breakdown = {"experience_validation": {"passed": True, "candidate_years": 5, "min_years": 4}}
    assert CampaignCandidateService._experience_gap_display(breakdown) == ""


def test_experience_gap_display_blank_when_missing():
    assert CampaignCandidateService._experience_gap_display({}) == ""


def test_education_gap_display_formats_required_and_found():
    breakdown = {
        "education_validation": {
            "passed": False,
            "required_level": "BACHELOR",
            "candidate_level": "DIPLOMA",
        }
    }
    result = CampaignCandidateService._education_gap_display(breakdown)
    assert "Bachelor" in result
    assert "found" in result


def test_education_gap_display_blank_when_passed():
    breakdown = {"education_validation": {"passed": True}}
    assert CampaignCandidateService._education_gap_display(breakdown) == ""
