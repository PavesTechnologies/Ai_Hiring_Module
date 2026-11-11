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
from tests.v2_breakdowns import with_v2_checks

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
        # v2's stored shape (retire v1, phase C): the checks and core-gate credit filled in.
        deterministic_breakdown=with_v2_checks(score_breakdown, passed=False) if score_breakdown else {},
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


def test_scorecard_score_breakdown_read_from_campaign_candidate_not_rejection_snapshot():
    breakdown = {"deterministic_score": 40.0, "deterministic_passed": False}
    candidate = _reject(_make_campaign_candidate(score_breakdown=breakdown))
    service = make_service(campaign_candidate_repo=_repo_returning(candidate))

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.score_breakdown == candidate.deterministic_breakdown


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
    assert "hr_override" not in row and "override_reason" not in row

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
    breakdown = with_v2_checks({
        "mandatory_skills": [
            {"canonical_name": "Python", "match_type": "MISSING"},
            {"canonical_name": "AWS", "match_type": "EXACT"},
            {"canonical_name": "Docker", "match_type": "MISSING"},
        ]
    })
    result = CampaignCandidateService._missing_mandatory_skills_display(breakdown)
    assert result == "Python, Docker"


def test_a_core_skill_found_but_not_confirmed_is_listed_as_missing_too():
    """v2: the gate counts confirmed core skills - one found but voted against or uncertain didn't count."""
    breakdown = with_v2_checks({"mandatory_skills": [
        {"canonical_name": "Python", "match_type": "EXACT", "counts_for_core_gate": False,
         "confirmation_status": "REJECTED"},
        {"canonical_name": "AWS", "match_type": "EXACT"},
        {"canonical_name": "Excel", "match_type": "MISSING", "importance": "supporting", "counts_for_core_gate": False},
    ]})
    assert CampaignCandidateService._missing_mandatory_skills_display(breakdown) == "Python"


def test_a_pre_phase_c_snapshot_is_read_through_its_nested_v2_breakdown():
    nested = with_v2_checks({"mandatory_skills": [{"canonical_name": "SQL", "match_type": "MISSING"}]})
    legacy = {"acted_on_version": "v2", "v2": nested,
              "mandatory_skills": [{"canonical_name": "SQL", "match_type": "EXACT"}]}   # v1's own view
    assert CampaignCandidateService._missing_mandatory_skills_display(legacy) == "SQL"


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


@pytest.mark.parametrize("breakdown, expected", [
    ({"awaiting_full_scoring": True, "needs_review": True}, True),     # a no-LLM fallback HOLD
    ({"awaiting_full_scoring": False, "full_scoring_recorded_only": True}, False),
    ({}, False),
])
def test_the_scorecard_says_whether_the_candidate_awaits_full_scoring(breakdown, expected):
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.HOLD, score_breakdown=breakdown)
    service = make_service(
        campaign_candidate_repo=_repo_returning(candidate),
        candidate_repo=MagicMock(get_by_id=MagicMock(return_value=None)),
        resume_repo=MagicMock(get_by_id=MagicMock(return_value=None)),
    )
    assert service.get_campaign_candidate_scorecard(candidate.id).awaiting_full_scoring is expected
