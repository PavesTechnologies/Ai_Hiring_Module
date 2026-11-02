"""Phase B against real Postgres (rolled back): the fallback columns, the waiting rows, and 'a person acted since'."""
from datetime import timedelta

from sqlalchemy import select

from app.models.outcome_overrides import CandidateOutcomeOverride
from app.models.pipeline import CampaignCandidateStageHistory, PipelineStage, TransitionSource
from app.models.scoring_v2 import CandidateScoringResult
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.tasks.full_scoring_tasks import human_acted_since
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def test_a_fallback_row_is_stored_and_found_while_it_waits(db, scored):  # noqa: F811
    cc = scored["cc"]
    row = CandidateScoringResult(campaign_candidate_id=cc.id, scoring_version="v2", scoring_config_version="t",
                                 acted_on=True, outcome="REVIEW", scoring_mode="no_llm", awaiting_full_scoring=True)
    db.add(row)
    db.flush()
    db.refresh(row)
    assert (row.scoring_mode, row.awaiting_full_scoring) == ("no_llm", True)
    plain = db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc.id,
                                                           CandidateScoringResult.scoring_version == "v1")).scalar_one()
    assert (plain.scoring_mode, plain.awaiting_full_scoring) == ("llm", False)     # the server defaults

    assert row.id in {r.id for r in ScoringResultRepository(db).awaiting_full_scoring(limit=1000)}
    row.awaiting_full_scoring = False
    db.flush()
    assert row.id not in {r.id for r in ScoringResultRepository(db).awaiting_full_scoring(limit=1000)}


def test_a_person_acting_since_the_fallback_is_seen_and_system_moves_are_not(db, scored):  # noqa: F811
    cc = scored["cc"]
    since = db.execute(select(CandidateOutcomeOverride.created_at)
                       .where(CandidateOutcomeOverride.campaign_candidate_id == cc.id)).scalar_one()
    # The fixture's own rows are from the same moment - nothing after it yet.
    assert human_acted_since(db, cc.id, since) is None

    db.add(CampaignCandidateStageHistory(campaign_candidate_id=cc.id, from_stage=PipelineStage.SCREENING,
                                         to_stage=PipelineStage.HOLD, transition_source=TransitionSource.SYSTEM,
                                         changed_at=since + timedelta(minutes=5)))
    db.flush()
    assert human_acted_since(db, cc.id, since) is None                    # the AI step's own HOLD

    db.add(CampaignCandidateStageHistory(campaign_candidate_id=cc.id, from_stage=PipelineStage.HOLD,
                                         to_stage=PipelineStage.SHORTLISTED, transition_source=TransitionSource.MANUAL,
                                         changed_at=since + timedelta(minutes=10)))
    db.flush()
    assert "moved to SHORTLISTED by a person" in human_acted_since(db, cc.id, since)
    assert human_acted_since(db, cc.id, since + timedelta(minutes=20)) is None


def test_an_override_since_the_fallback_counts_as_a_person_acting(db, scored):  # noqa: F811
    cc = scored["cc"]
    override = db.execute(select(CandidateOutcomeOverride)
                          .where(CandidateOutcomeOverride.campaign_candidate_id == cc.id)).scalar_one()
    assert "overridden (SKILL_EVIDENCE_MISSED)" in human_acted_since(db, cc.id, override.created_at - timedelta(hours=1))
