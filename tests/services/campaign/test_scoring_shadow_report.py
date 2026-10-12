"""Scoring v2 Step 4: shadow report ranks, disagreement flags, outcome labels, recruiter ranking."""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign.scoring_shadow_report_service import (
    NOT_SCORED, ScoringShadowReportService, annotate, rank_by, v1_label, v2_label,
)


def _row(name, v1, v1_outcome, v2, v2_outcome, recruiter=None):
    return {"campaign_candidate_id": name, "v1_score": v1, "v1_outcome": v1_outcome, "v2_score": v2,
            "v2_outcome": v2_outcome, "recruiter_rank": recruiter}


def test_rank_by_score_with_ties_and_unscored():
    rows = [_row("a", 50, "PASS", None, NOT_SCORED), _row("b", 70, "PASS", None, NOT_SCORED),
            _row("c", 50, "PASS", None, NOT_SCORED), _row("d", None, NOT_SCORED, None, NOT_SCORED)]
    assert rank_by(rows, "v1_score") == {"b": 1, "a": 2, "c": 2}


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
    assert by["cand_b"]["disagreements"][0] == "v1 rejects, v2 review"
    assert by["cand_c"]["disagreements"][0] == "v1 passes, v2 rejects"
    assert by["cand_a"]["v2_rank"] == 1 and by["cand_a"]["v1_rank"] == 3
    assert not any("v1 rejects" in f or "v1 passes" in f for f in by["cand_d"]["disagreements"])
    # ranks far from the recruiter's are flagged too
    assert any("v1 rank" in f and "recruiter 6" in f for f in by["cand_c"]["disagreements"])


def test_pending_and_not_run_never_count_as_outcome_disagreements():
    rows = annotate([_row("x", 60, "PASS", None, "PENDING"), _row("y", 10, "REJECT", None, "NOT_RUN")])
    assert all(not r["disagreements"] for r in rows)


def test_outcome_labels():
    assert v2_label(None) == NOT_SCORED
    assert v2_label(SimpleNamespace(outcome="REJECT", breakdown={"would_be_review": True})) == "REVIEW"
    assert v2_label(SimpleNamespace(outcome="PENDING", breakdown={})) == "PENDING"
    assert v1_label(None, SimpleNamespace(deterministic_passed=None)) == NOT_SCORED
    assert v1_label(None, SimpleNamespace(deterministic_passed=False)) == "REJECT"
    assert v1_label(SimpleNamespace(outcome="PASS"), SimpleNamespace(deterministic_passed=False)) == "PASS"


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
