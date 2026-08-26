from types import SimpleNamespace
from uuid import uuid4

from app.models.pipeline import DecisionSource, PipelineStage
from app.services.campaign.campaign_candidate_service import CampaignCandidateService

"""
Bug fix - a candidate rejected at DETERMINISTIC never has semantic scoring
enqueued at all (deterministic_scoring_tasks.py only calls
_enqueue_semantic_scoring on a deterministic PASS), so semantic_breakdown
stays NULL forever - previously indistinguishable from "hasn't reached
semantic yet" on the Semantic tab. _build_semantic_score_breakdown is a
@staticmethod, called directly here (no CampaignCandidateService
instantiation needed) - sidesteps unrelated, pre-existing breakage in
test_campaign_candidate_semantic_endpoint.py (that whole file's
make_service() helper passes a candidate_rejection_repo kwarg the real
constructor no longer accepts at all).
"""


def _cc(pipeline_stage, decision_source, semantic_breakdown=None):
    return SimpleNamespace(
        id=uuid4(), pipeline_stage=pipeline_stage, decision_source=decision_source,
        semantic_breakdown=semantic_breakdown,
    )


def test_returns_not_applicable_when_rejected_at_deterministic():
    cc = _cc(PipelineStage.REJECTED, DecisionSource.DETERMINISTIC)

    result = CampaignCandidateService._build_semantic_score_breakdown(cc)

    assert result is not None
    assert result.summary.status == "NOT_APPLICABLE"
    assert result.semantic_passed is None
    assert result.matching_skills == []


def test_stays_null_when_rejected_at_semantic_itself():
    """A SEMANTIC rejection means semantic actually ran - breakdown should be populated, not synthesized, and this helper is never reached with a null breakdown in that case."""
    cc = _cc(PipelineStage.REJECTED, DecisionSource.SEMANTIC, semantic_breakdown=None)

    result = CampaignCandidateService._build_semantic_score_breakdown(cc)

    assert result is None


def test_stays_null_when_not_yet_processed():
    cc = _cc(PipelineStage.SCREENING, None)

    result = CampaignCandidateService._build_semantic_score_breakdown(cc)

    assert result is None


def test_stays_null_when_rejected_at_a_later_layer_than_deterministic():
    cc = _cc(PipelineStage.REJECTED, DecisionSource.AI)

    result = CampaignCandidateService._build_semantic_score_breakdown(cc)

    assert result is None


def test_real_breakdown_still_takes_priority_over_the_not_applicable_check():
    breakdown = {
        "semantic_score": 0.7, "overall_similarity": 0.7, "semantic_passed": True,
        "semantic_threshold": 0.65, "matching_skills": ["Python"], "missing_skills": [],
        "matched_keywords": [],
    }
    cc = _cc(PipelineStage.REJECTED, DecisionSource.DETERMINISTIC, semantic_breakdown=breakdown)

    result = CampaignCandidateService._build_semantic_score_breakdown(cc)

    assert result.summary.status == "PASSED"
    assert result.matching_skills == ["Python"]
