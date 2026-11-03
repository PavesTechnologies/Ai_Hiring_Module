"""Shadow mode (retire v1, phase D): current vs candidate ranks, disagreement flags, outcome labels, recruiter ranking."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign.scoring_shadow_report_service import (
    NOT_SCORED, ScoringShadowReportService, annotate, outcome_label, rank_by,
)


def _row(name, current, current_outcome, candidate, candidate_outcome, recruiter=None):
    return {"campaign_candidate_id": name, "current_score": current, "current_outcome": current_outcome,
            "candidate_score": candidate, "candidate_outcome": candidate_outcome, "recruiter_rank": recruiter}


def test_rank_by_score_with_ties_and_unscored():
    rows = [_row("a", 50, "PASS", None, NOT_SCORED), _row("b", 70, "PASS", None, NOT_SCORED),
            _row("c", 50, "PASS", None, NOT_SCORED), _row("d", None, NOT_SCORED, None, NOT_SCORED)]
    assert rank_by(rows, "current_score") == {"b": 1, "a": 2, "c": 2}


def test_disagreements_are_flagged():
    rows = annotate([
        _row("cand_a", 22.5, "PASS", 67.0, "PASS", recruiter=1),
        _row("cand_b", 18.8, "REJECT", 29.7, "REVIEW", recruiter=3),
        _row("cand_c", 51.8, "PASS", 46.2, "REJECT", recruiter=6),
        _row("cand_d", 21.2, "REJECT", 42.5, "REJECT", recruiter=2),
        _row("cand_e", 20.0, "REJECT", 33.3, "REJECT", recruiter=5),
        _row("cand_f", 27.5, "REJECT", 34.8, "REJECT", recruiter=4),
    ])
    by = {r["campaign_candidate_id"]: r for r in rows}
    assert by["cand_b"]["disagreements"][0] == "current reject, candidate review"
    assert by["cand_c"]["disagreements"][0] == "current pass, candidate reject"
    assert by["cand_a"]["candidate_rank"] == 1 and by["cand_a"]["current_rank"] == 3
    assert not any(f.startswith("current ") and "candidate" in f for f in by["cand_d"]["disagreements"])
    # ranks far from the recruiter's are flagged too
    assert any("current rank" in f and "recruiter 6" in f for f in by["cand_c"]["disagreements"])


def test_pending_and_not_run_never_count_as_outcome_disagreements():
    rows = annotate([_row("x", 60, "PASS", None, "PENDING"), _row("y", 10, "REJECT", None, "NOT_RUN")])
    assert all(not r["disagreements"] for r in rows)


def test_outcome_labels():
    assert outcome_label(None) == NOT_SCORED
    assert outcome_label(SimpleNamespace(outcome="REJECT", breakdown={"would_be_review": True})) == "REVIEW"
    assert outcome_label(SimpleNamespace(outcome="PENDING", breakdown={})) == "PENDING"


def _result(cc_id, *, acted_on, version_id, score, outcome, minutes_ago):
    return SimpleNamespace(campaign_candidate_id=cc_id, acted_on=acted_on, scoring_version_id=version_id,
                           score=score, outcome=outcome, breakdown={}, scoring_mode="llm",
                           scoring_config_version="h", created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


def test_latest_splits_the_decision_from_the_candidate_versions_result():
    """Current = the latest result acted on; candidate = the latest of the campaign's candidate version only."""
    cc, tried, other = uuid4(), uuid4(), uuid4()
    rows = [  # newest first, as the query orders them
        _result(cc, acted_on=False, version_id=other, score=10, outcome="REJECT", minutes_ago=1),
        _result(cc, acted_on=False, version_id=tried, score=55, outcome="PASS", minutes_ago=2),
        _result(cc, acted_on=True, version_id=uuid4(), score=40, outcome="REJECT", minutes_ago=3),
        _result(cc, acted_on=False, version_id=tried, score=20, outcome="REJECT", minutes_ago=4),
    ]
    service = ScoringShadowReportService.__new__(ScoringShadowReportService)
    service.db = MagicMock()
    service.db.execute.return_value.scalars.return_value.all.return_value = rows
    current, candidate = service._latest([cc], tried)
    assert (current[cc].score, candidate[cc].score) == (40, 55)
    current, candidate = service._latest([cc], None)
    assert candidate == {} and current[cc].score == 40


def test_recruiter_ranking_only_for_candidates_in_the_campaign():
    service = ScoringShadowReportService.__new__(ScoringShadowReportService)
    service.db = MagicMock()
    service.campaign_repo = MagicMock(get_by_id=MagicMock(return_value=SimpleNamespace(id=uuid4())))
    service.audit_service = MagicMock()
    service.db.execute.return_value.all.return_value = [(uuid4(),)]
    service.db.execute.return_value.scalars.return_value = []
    with pytest.raises(CampaignException) as exc:
        service.save_recruiter_ranking(uuid4(), rankings=[{"campaign_candidate_id": str(uuid4()), "rank": 1}],
                                       ranked_by_name="Hiring lead", actor_id="u1", actor_role="HR_ADMIN")
    assert exc.value.status_code == 422
    service.db.commit.assert_not_called()
