from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.pipeline import AIEvaluationStatus, CompositeScoreTriggerSource, PipelineStage, TransitionSource
from app.repositories.candidate_composite_score_history_repository import (
    CandidateCompositeScoreHistoryRepository,
)
from app.schemas.campaign.campaign_candidate_schema import CandidateCompositeResponse
from app.services.campaign.campaign_candidate_service import CampaignCandidateService


def make_service(campaign_repo=None, campaign_candidate_repo=None, audit_service=None, composite_score_history_repo=None):
    return CampaignCandidateService(
        campaign_repo=campaign_repo or MagicMock(),
        campaign_candidate_repo=campaign_candidate_repo or MagicMock(),
        audit_service=audit_service or MagicMock(),
        composite_score_history_repo=composite_score_history_repo,
    )


def _make_campaign_candidate(
    composite_score=None, deterministic_score=None, semantic_score=None, effective_ai_score=None,
    ai_evaluation_status=AIEvaluationStatus.PENDING, pipeline_stage=PipelineStage.SCREENING,
    composite_score_computed_at=None, hr_override=False, hr_override_by=None,
    hr_override_reason=None, hr_override_at=None,
):
    campaign_id = uuid4()
    return SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id, pipeline_stage=pipeline_stage,
        composite_score=composite_score, deterministic_score=deterministic_score,
        semantic_score=semantic_score, effective_ai_score=effective_ai_score,
        ai_evaluation_status=ai_evaluation_status, composite_score_computed_at=composite_score_computed_at,
        hr_override=hr_override, hr_override_by=hr_override_by,
        hr_override_reason=hr_override_reason, hr_override_at=hr_override_at,
    )


def _make_campaign(campaign_id, weight_deterministic=30.0, weight_semantic=40.0, weight_ai=30.0):
    return SimpleNamespace(id=campaign_id, weight_deterministic=weight_deterministic,
        weight_semantic=weight_semantic, weight_ai=weight_ai)


def _make_stage_history_row(
    from_stage=None, to_stage=PipelineStage.SCREENING, transition_source=TransitionSource.SYSTEM,
    changed_by=None, change_reason=None, scores_snapshot=None,
):
    return SimpleNamespace(
        from_stage=from_stage, to_stage=to_stage, transition_source=transition_source,
        changed_by=changed_by, changed_at=datetime.now(timezone.utc),
        change_reason=change_reason, scores_snapshot=scores_snapshot,
    )


def _make_composite_history_row(
    formula_version="v1", trigger_source=CompositeScoreTriggerSource.AI_EVALUATION,
    composite_score=75.5, weight_deterministic=30.0, weight_semantic=40.0, weight_ai=30.0,
    deterministic_score=80.0, semantic_score=0.7, normalized_semantic_score=70.0, effective_ai_score=60.0,
):
    return SimpleNamespace(
        calculated_at=datetime.now(timezone.utc), trigger_source=trigger_source, formula_version=formula_version,
        weight_deterministic=weight_deterministic, weight_semantic=weight_semantic, weight_ai=weight_ai,
        deterministic_score=deterministic_score, semantic_score=semantic_score,
        normalized_semantic_score=normalized_semantic_score, effective_ai_score=effective_ai_score,
        composite_score=composite_score,
    )


# ----------------------------------------------------------------------
# Constructor / backward compatibility
# ----------------------------------------------------------------------

def test_constructor_derives_composite_score_history_repo_from_campaign_candidate_repo_when_omitted():
    """Every pre-existing call site never passes composite_score_history_repo - must default safely."""
    db = MagicMock()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.db = db

    service = CampaignCandidateService(
        campaign_repo=MagicMock(), campaign_candidate_repo=campaign_candidate_repo, audit_service=MagicMock(),
    )

    assert isinstance(service.composite_score_history_repo, CandidateCompositeScoreHistoryRepository)
    assert service.composite_score_history_repo.db is db


# ----------------------------------------------------------------------
# get_candidate_timeline (Story 1)
# ----------------------------------------------------------------------

def test_timeline_raises_when_campaign_candidate_not_found():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_timeline(uuid4())

    assert exc_info.value.status_code == 404


def test_timeline_empty_returns_current_stage_with_no_events():
    cc = _make_campaign_candidate(pipeline_stage=PipelineStage.SCREENING)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = []
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_timeline(cc.id)

    assert result.campaign_candidate_id == cc.id
    assert result.current_stage == PipelineStage.SCREENING
    assert result.events == []


def test_timeline_maps_events_exactly_reusing_existing_enums():
    cc = _make_campaign_candidate(pipeline_stage=PipelineStage.REJECTED)
    row = _make_stage_history_row(
        from_stage=PipelineStage.SCREENING, to_stage=PipelineStage.REJECTED,
        transition_source=TransitionSource.SYSTEM, changed_by="system", change_reason="Deterministic filter rejection",
        scores_snapshot={"deterministic_score": 40.0},
    )
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = [row]
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_timeline(cc.id)

    event = result.events[0]
    assert event.from_stage == PipelineStage.SCREENING
    assert event.to_stage == PipelineStage.REJECTED
    assert event.transition_source == TransitionSource.SYSTEM
    assert event.changed_by == "system"
    assert event.comments == "Deterministic filter rejection"
    assert event.metadata == {"deterministic_score": 40.0}


def test_timeline_never_writes_audit_entry():
    cc = _make_campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = []
    audit_service = MagicMock()
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, audit_service=audit_service)

    service.get_candidate_timeline(cc.id)

    audit_service.log.assert_not_called()


def test_timeline_supports_large_history():
    cc = _make_campaign_candidate()
    rows = [_make_stage_history_row() for _ in range(500)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_candidate_repo.get_stage_history_by_campaign_candidate_id.return_value = rows
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    result = service.get_candidate_timeline(cc.id)

    assert len(result.events) == 500


# ----------------------------------------------------------------------
# get_candidate_composite_history (Story 2)
# ----------------------------------------------------------------------

def test_composite_history_raises_when_campaign_candidate_not_found():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_composite_history(uuid4())

    assert exc_info.value.status_code == 404


def test_composite_history_empty_when_composite_never_calculated():
    cc = _make_campaign_candidate(composite_score=None)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, composite_score_history_repo=history_repo)

    result = service.get_candidate_composite_history(cc.id)

    assert result.entries == []


def test_composite_history_returns_data_exactly_as_stored():
    cc = _make_campaign_candidate()
    row = _make_composite_history_row(formula_version="v1", composite_score=67.42)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = [row]
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, composite_score_history_repo=history_repo)

    result = service.get_candidate_composite_history(cc.id)

    entry = result.entries[0]
    assert entry.formula_version == "v1"
    assert entry.composite_score == 67.42
    assert entry.trigger_source == CompositeScoreTriggerSource.AI_EVALUATION
    assert entry.weight_deterministic == 30.0
    assert entry.computed_by == "SYSTEM"


def test_composite_history_never_writes_audit_or_mutates():
    cc = _make_campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    audit_service = MagicMock()
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, composite_score_history_repo=history_repo,
        audit_service=audit_service,
    )

    service.get_candidate_composite_history(cc.id)

    audit_service.log.assert_not_called()
    history_repo.create.assert_not_called()


def test_composite_history_supports_large_history():
    cc = _make_campaign_candidate()
    rows = [_make_composite_history_row() for _ in range(500)]
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = rows
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, composite_score_history_repo=history_repo)

    result = service.get_candidate_composite_history(cc.id)

    assert len(result.entries) == 500


# ----------------------------------------------------------------------
# get_candidate_ranking_details (Story 3)
# ----------------------------------------------------------------------

def test_ranking_details_raises_when_campaign_candidate_not_found():
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_ranking_details(uuid4())

    assert exc_info.value.status_code == 404


def test_ranking_details_raises_when_campaign_not_found():
    cc = _make_campaign_candidate()
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = None
    service = make_service(campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo)

    with pytest.raises(CampaignException) as exc_info:
        service.get_candidate_ranking_details(cc.id)

    assert exc_info.value.status_code == 404


def test_ranking_details_uses_current_campaign_weights_not_history_weights():
    cc = _make_campaign_candidate(composite_score=70.0, effective_ai_score=60.0)
    campaign = _make_campaign(cc.campaign_id, weight_deterministic=50.0, weight_semantic=30.0, weight_ai=20.0)
    history_row = _make_composite_history_row(weight_deterministic=30.0, weight_semantic=40.0, weight_ai=30.0)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = [history_row]
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    # Current campaign weights (50/30/20), NOT the history row's weights (30/40/30).
    assert result.weight_deterministic == 50.0
    assert result.weight_semantic == 30.0
    assert result.weight_ai == 20.0


def test_ranking_details_ai_evaluation_score_maps_from_effective_ai_score():
    cc = _make_campaign_candidate(effective_ai_score=62.5)
    campaign = _make_campaign(cc.campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.ai_evaluation_score == 62.5


def test_ranking_details_formula_version_none_when_composite_never_calculated():
    cc = _make_campaign_candidate(composite_score=None)
    campaign = _make_campaign(cc.campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.formula_version is None
    assert result.ranking_status == "PENDING"


def test_ranking_details_formula_version_from_most_recent_history_row():
    cc = _make_campaign_candidate(composite_score=70.0)
    campaign = _make_campaign(cc.campaign_id)
    newest = _make_composite_history_row(formula_version="v2")
    older = _make_composite_history_row(formula_version="v1")
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    # get_by_campaign_candidate_id is documented most-recent-first.
    history_repo.get_by_campaign_candidate_id.return_value = [newest, older]
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.formula_version == "v2"


def test_ranking_details_reuses_derive_ranking_status_verbatim():
    cc = _make_campaign_candidate(composite_score=None, ai_evaluation_status=AIEvaluationStatus.FAILED)
    campaign = _make_campaign(cc.campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.ranking_status == "FAILED"


def test_ranking_details_includes_hr_override_state():
    hr_override_at = datetime.now(timezone.utc)
    cc = _make_campaign_candidate(
        hr_override=True, hr_override_by="hr-1", hr_override_reason="Mistaken rejection",
        hr_override_at=hr_override_at,
    )
    campaign = _make_campaign(cc.campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.hr_override is True
    assert result.hr_override_by == "hr-1"
    assert result.hr_override_reason == "Mistaken rejection"
    assert result.hr_override_at == hr_override_at


def test_ranking_details_missing_hr_override_defaults_gracefully():
    """Legacy SimpleNamespace fixtures without hr_override fields must not crash (getattr-guarded)."""
    campaign_id = uuid4()
    cc = SimpleNamespace(
        id=uuid4(), campaign_id=campaign_id, composite_score=70.0, deterministic_score=80.0,
        semantic_score=0.7, effective_ai_score=60.0, ai_evaluation_status=AIEvaluationStatus.COMPLETED,
    )
    campaign = _make_campaign(campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.hr_override is False
    assert result.hr_override_by is None
    assert result.composite_score_computed_at is None


def test_ranking_details_never_writes_audit_entry():
    cc = _make_campaign_candidate()
    campaign = _make_campaign(cc.campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    audit_service = MagicMock()
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo, audit_service=audit_service,
    )

    service.get_candidate_ranking_details(cc.id)

    audit_service.log.assert_not_called()


def test_ranking_details_does_not_recompute_composite_score():
    """The exact stored composite_score is returned - CompositeScoringService is never called."""
    cc = _make_campaign_candidate(composite_score=42.42)
    campaign = _make_campaign(cc.campaign_id)
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = []
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_ranking_details(cc.id)

    assert result.composite_score == 42.42


# ----------------------------------------------------------------------
# get_candidate_composite
# ----------------------------------------------------------------------

def test_composite_returns_tab_response_without_override_fields():
    computed_at = datetime.now(timezone.utc)
    cc = _make_campaign_candidate(
        composite_score=86.25,
        deterministic_score=80.0,
        semantic_score=0.9,
        composite_score_computed_at=computed_at,
    )
    cc.ai_evaluation = SimpleNamespace(effective_ai_score=95.0)
    campaign = _make_campaign(cc.campaign_id, weight_deterministic=40.0, weight_semantic=35.0, weight_ai=25.0)
    history_row = _make_composite_history_row(formula_version="v2")
    campaign_candidate_repo = MagicMock()
    campaign_candidate_repo.get_by_id.return_value = cc
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign
    history_repo = MagicMock()
    history_repo.get_by_campaign_candidate_id.return_value = [history_row]
    service = make_service(
        campaign_candidate_repo=campaign_candidate_repo, campaign_repo=campaign_repo,
        composite_score_history_repo=history_repo,
    )

    result = service.get_candidate_composite(cc.id)

    assert isinstance(result, CandidateCompositeResponse)
    assert result.campaign_candidate_id == cc.id
    assert result.composite_score == 86.25
    assert result.deterministic_score == 80.0
    assert result.semantic_score == 0.9
    assert result.ai_evaluation_score == 95.0
    assert result.weight_deterministic == 40.0
    assert result.weight_semantic == 35.0
    assert result.weight_ai == 25.0
    assert result.formula_version == "v2"
    assert result.ranking_status == "RANKED"
    assert result.composite_score_computed_at == computed_at
    assert not hasattr(result, "hr_override")
