from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import PipelineStage, RejectionLayer
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


def _make_campaign_candidate(
    pipeline_stage=PipelineStage.REJECTED,
    score_breakdown=None,
    hr_override=False,
    hr_override_reason=None,
    deterministic_score=42.5,
):
    return SimpleNamespace(
        id=uuid4(),
        campaign_id=uuid4(),
        candidate_id=uuid4(),
        resume_id=uuid4(),
        pipeline_stage=pipeline_stage,
        score_breakdown=score_breakdown or {},
        hr_override=hr_override,
        hr_override_reason=hr_override_reason,
        deterministic_score=deterministic_score,
        ai_ats_score=None,
        semantic_score=None,
        composite_score=None,
        created_at=datetime.now(timezone.utc),
    )


def _make_rejection(rejection_layer=RejectionLayer.DETERMINISTIC, reason="Missing required skills: Python."):
    return SimpleNamespace(
        id=uuid4(),
        rejection_layer=rejection_layer,
        rejection_reason=reason,
        rejected_at=datetime.now(timezone.utc),
    )


def make_service(
    campaign_candidate_repo=None,
    candidate_rejection_repo=None,
    candidate_repo=None,
    resume_repo=None,
    campaign_repo=None,
    audit_service=None,
):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
        encryption_service=MagicMock(),
        candidate_repo=candidate_repo,
        resume_repo=resume_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )


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
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    rejection = _make_rejection(rejection_layer=RejectionLayer.DETERMINISTIC)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        candidate_repo=MagicMock(get_by_id=MagicMock(return_value=None)),
        resume_repo=MagicMock(get_by_id=MagicMock(return_value=None)),
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert isinstance(scorecard, CandidateScorecardResponse)
    assert scorecard.has_rejection is True
    assert scorecard.rejection_layer == RejectionLayer.DETERMINISTIC
    assert scorecard.rejection_reason == rejection.rejection_reason
    assert scorecard.rejected_at == rejection.rejected_at
    assert scorecard.is_overridden is False
    assert scorecard.status is None


def test_scorecard_banner_absent_when_rejection_layer_is_not_deterministic():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    rejection = _make_rejection(rejection_layer=RejectionLayer.SEMANTIC)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.has_rejection is False
    assert scorecard.rejection_layer is None
    assert scorecard.rejection_reason is None


def test_scorecard_banner_absent_when_pipeline_stage_is_not_rejected():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.has_rejection is False
    candidate_rejection_repo.get_by_campaign_candidate_id.assert_not_called()


def test_scorecard_reports_overridden_status_while_preserving_original_reason():
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, hr_override=True)
    rejection = _make_rejection(rejection_layer=RejectionLayer.DETERMINISTIC, reason="Insufficient experience: 2 years provided, minimum 4 years required (gap: 2 years).")

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.is_overridden is True
    assert scorecard.status == "Overridden — Previously Rejected"
    # Original rejection reason/timestamp must be preserved, never rewritten.
    assert scorecard.rejection_reason == rejection.rejection_reason
    assert scorecard.rejected_at == rejection.rejected_at


def test_scorecard_preserves_rejection_reason_after_a_real_override_moves_stage_off_rejected():
    """
    M07-E03 S04: an applied override moves pipeline_stage to SCREENING, so
    the old `pipeline_stage == REJECTED` gate alone would wipe out the
    "preserved original rejection_reason/rejected_at" guarantee this
    story's own T01 spec requires. has_rejection itself still reflects
    "currently REJECTED" (False here), but is_overridden/status/
    rejection_reason/rejected_at must still be populated.
    """
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING, hr_override=True)
    rejection = _make_rejection(rejection_layer=RejectionLayer.DETERMINISTIC, reason="Missing required skills: AWS.")

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.has_rejection is False  # not currently in the REJECTED stage
    assert scorecard.is_overridden is True
    assert scorecard.status == "Overridden — Previously Rejected"
    assert scorecard.rejection_reason == rejection.rejection_reason
    assert scorecard.rejected_at == rejection.rejected_at
    assert scorecard.rejection_layer == RejectionLayer.DETERMINISTIC


def test_scorecard_score_breakdown_read_from_campaign_candidate_not_rejection_snapshot():
    breakdown = {"deterministic_score": 40.0, "deterministic_passed": False}
    candidate = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED, score_breakdown=breakdown)
    rejection = _make_rejection()

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    scorecard = service.get_campaign_candidate_scorecard(candidate.id)

    assert scorecard.score_breakdown == breakdown


# ----------------------------------------------------------------------
# T02 - Show Rejection History
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
    newest = _make_rejection(reason="Missing required skills: AWS.")
    oldest = _make_rejection(reason="Missing required skills: Python.")

    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate
    candidate_rejection_repo = MagicMock()
    # Repository already returns newest-first.
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [newest, oldest]

    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
    )

    history = service.get_rejection_history(candidate.id)

    assert len(history) == 2
    assert all(isinstance(entry, CandidateRejectionHistoryEntryResponse) for entry in history)
    assert history[0].id == newest.id
    assert history[0].current_status is True
    assert history[0].evaluation_round == 2
    assert history[1].id == oldest.id
    assert history[1].current_status is False
    assert history[1].evaluation_round == 1


def test_rejection_history_returns_empty_list_when_repo_not_configured():
    candidate = _make_campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = candidate

    service = make_service(campaign_candidate_repo=campaign_candidate_repo, candidate_rejection_repo=None)

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
    candidate_1 = _make_campaign_candidate(score_breakdown=breakdown, hr_override=True, hr_override_reason="HR approved")
    rejection = _make_rejection(reason="Missing required skills: Python.")

    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_rejected_by_campaign.return_value = [candidate_1]
    candidate_rejection_repo = MagicMock()
    candidate_rejection_repo.get_by_campaign_candidate_id.return_value = [rejection]
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

    service = make_service(
        campaign_repo=campaign_repo,
        campaign_candidate_repo=campaign_candidate_repo,
        candidate_rejection_repo=candidate_rejection_repo,
        audit_service=audit_service,
    )

    response = service.export_rejected_candidates(campaign.id, actor_id="user-1", actor_role="HR_ADMIN")

    rows = captured_rows["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["candidate_uuid"] == str(candidate_1.candidate_id)
    assert row["rejection_layer"] == RejectionLayer.DETERMINISTIC.value
    assert row["rejection_reason"] == rejection.rejection_reason
    assert row["missing_mandatory_skills"] == "Python"
    assert "2.5 years provided, 4 years required" in row["experience_gap"]
    assert row["education_gap"] == ""
    assert row["hr_override"] is True
    assert row["override_reason"] == "HR approved"

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
