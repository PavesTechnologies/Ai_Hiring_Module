from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.repositories.campaign_candidate_repository import CampaignCandidateRepository

"""
Bug fix - reset_for_rescore previously shared reset_for_resubmission's
pre-existing gap where semantic_breakdown/semantic_score_computed_at
survive a reset even though semantic_score is cleared. Found live: a
candidate re-scored from FRAUD_REVIEW kept showing its prior PASSED
semantic breakdown (with a screened_at from the earlier run) alongside a
NULL semantic_score. Fixed on reset_for_rescore only - reset_for_resubmission's
own copy of this gap is untouched, out of scope for this fix.
"""


def _scored_candidate():
    return SimpleNamespace(
        id=uuid4(), screened_at="2026-08-01T00:00:00Z",
        deterministic_score=0.9, deterministic_passed=True, deterministic_breakdown={"x": 1},
        semantic_score=0.68, semantic_passed=True,
        semantic_breakdown={"overall_similarity": 0.68}, semantic_score_computed_at="2026-08-01T00:00:00Z",
        composite_score=0.8, composite_score_computed_at="2026-08-01T00:00:00Z",
        fraud_flags=["x"], is_fraud_flagged=True,
        decision_type="AUTO_REJECT", decision_source="SYSTEM", decision_reason="x",
        decision_details={"x": 1}, decision_by_user_id=uuid4(), decision_at="2026-08-01T00:00:00Z",
    )


def test_reset_for_rescore_clears_semantic_breakdown_and_computed_at():
    repo = CampaignCandidateRepository(MagicMock())
    cc = _scored_candidate()

    repo.reset_for_rescore(cc)

    assert cc.semantic_score is None
    assert cc.semantic_passed is None
    assert cc.semantic_breakdown is None
    assert cc.semantic_score_computed_at is None


def test_reset_for_rescore_clears_every_other_evaluation_derived_field():
    repo = CampaignCandidateRepository(MagicMock())
    cc = _scored_candidate()

    repo.reset_for_rescore(cc)

    assert cc.screened_at is None
    assert cc.deterministic_score is None
    assert cc.deterministic_passed is None
    assert cc.deterministic_breakdown is None
    assert cc.composite_score is None
    assert cc.composite_score_computed_at is None
    assert cc.fraud_flags is None
    assert cc.is_fraud_flagged is False
    assert cc.decision_type is None
    assert cc.decision_source is None
    assert cc.decision_reason is None
    assert cc.decision_details is None
    assert cc.decision_by_user_id is None
    assert cc.decision_at is None
