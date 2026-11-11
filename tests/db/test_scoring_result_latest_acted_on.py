"""The HR core-skill panel reads the latest acted-on result, never a newer shadow (candidate version) row."""
from datetime import datetime, timedelta, timezone

from app.models.scoring_v2 import CandidateScoringResult
from app.repositories.scoring_result_repository import ScoringResultRepository
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def _row(cc_id, *, acted_on, outcome, at):
    return CandidateScoringResult(campaign_candidate_id=cc_id, scoring_version="v2", scoring_config_version="t",
                                  acted_on=acted_on, outcome=outcome, created_at=at)


def test_latest_acted_on_skips_a_newer_shadow_row(db, scored):  # noqa: F811
    cc = scored["cc"]
    base = datetime.now(timezone.utc) + timedelta(days=1)          # after the fixture's own rows
    live = _row(cc.id, acted_on=True, outcome="PASS", at=base)
    shadow = _row(cc.id, acted_on=False, outcome="REJECT", at=base + timedelta(minutes=5))
    db.add_all([live, shadow])
    db.flush()

    repo = ScoringResultRepository(db)
    assert repo.latest(cc.id, "v2").id == shadow.id                 # why the panel must not use latest()
    assert repo.latest_acted_on(cc.id).id == live.id


def test_latest_acted_on_is_none_when_only_shadow_rows_exist(db, scored):  # noqa: F811
    cc = scored["cc"]
    for row in db.query(CandidateScoringResult).filter_by(campaign_candidate_id=cc.id).all():
        row.acted_on = False
    db.flush()
    assert ScoringResultRepository(db).latest_acted_on(cc.id) is None
