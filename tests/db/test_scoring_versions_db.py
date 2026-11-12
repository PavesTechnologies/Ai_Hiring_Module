"""Scoring versions against real Postgres (rolled back): one current at a time, promotion, results tagged."""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.scoring_v2 import CandidateScoringResult, ScoringVersion
from app.services.campaign.scoring_versions import (
    STATUS_CANDIDATE, STATUS_CURRENT, STATUS_RETIRED, ScoringVersionService, current_version,
)
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def test_the_database_allows_only_one_current_version(db):  # noqa: F811
    assert current_version(db) is not None                       # the migration's / the seed's
    db.add(ScoringVersion(id=uuid4(), label=f"second current {uuid4()}", status=STATUS_CURRENT, configuration={}))
    with pytest.raises(IntegrityError):
        db.flush()


def test_promoting_retires_the_current_version_first_and_stops_the_campaigns_trying_it(db, scored):  # noqa: F811
    campaign = scored["campaign"]
    old = current_version(db)
    service = ScoringVersionService(db, MagicMock())
    created = service.create(label=f"core x3 {uuid4()}", configuration={"importance_weights": {"core": 3.0}},
                             model=None, note=None, actor_id=None, actor_role="HR_ADMIN")
    service.set_campaign_candidate(campaign.id, version_id=created["id"], backfill=False, actor_id=None,
                                   actor_role="HR_ADMIN")
    assert db.get(type(campaign), campaign.id).candidate_scoring_version_id is not None
    _passed_golden_run(db, created["id"])

    service.promote(created["id"], actor_id=None, actor_role="HR_ADMIN")

    db.expire_all()
    assert [v.status for v in db.execute(select(ScoringVersion).where(
        ScoringVersion.status == STATUS_CURRENT)).scalars()] == [STATUS_CURRENT]
    assert current_version(db).label == created["label"]
    assert db.get(ScoringVersion, old.id).status == STATUS_RETIRED
    assert db.get(type(campaign), campaign.id).candidate_scoring_version_id is None


def test_a_result_row_records_its_scoring_version(db, scored):  # noqa: F811
    cc = scored["cc"]
    version = ScoringVersion(id=uuid4(), label=f"tagged {uuid4()}", status=STATUS_CANDIDATE, configuration={})
    db.add(version)
    db.flush()
    row = CandidateScoringResult(campaign_candidate_id=cc.id, scoring_version="v2", score=50, passed=True,
                                 outcome="PASS", breakdown={}, scoring_config_version="h", config_snapshot={},
                                 acted_on=False, scoring_version_id=version.id)
    db.add(row)
    db.flush()
    db.expire_all()
    assert db.get(CandidateScoringResult, row.id).scoring_version_id == version.id


def _passed_golden_run(db, version_id):  # noqa: F811
    """Settings phase 3: promotion needs a PASSED golden run for the version's exact configuration."""
    from datetime import datetime, timezone
    from app.models.scoring_v2 import ScoringVersionGoldenRun
    from app.services.settings.golden_runs import config_hash
    version = db.get(ScoringVersion, version_id if not isinstance(version_id, str) else __import__("uuid").UUID(version_id))
    run = ScoringVersionGoldenRun(scoring_version_id=version.id, config_hash=config_hash(version), purpose="PROMOTION",
                                  kind="RUN", status="PASSED", llm_calls=0, finished_at=datetime.now(timezone.utc))
    db.add(run)
    db.flush()
    return run


def test_promotion_needs_a_passing_golden_run_for_the_exact_configuration(db):  # noqa: F811
    from app.exceptions.campaign_exceptions import CampaignException
    service = ScoringVersionService(db, MagicMock())
    created = service.create(label=f"core x4 {uuid4()}", configuration={"importance_weights": {"core": 4.0}},
                             model=None, note=None, actor_id=None, actor_role="HR_ADMIN")
    with pytest.raises(CampaignException) as exc:                       # no run at all
        service.promote(created["id"], actor_id=None, actor_role="HR_ADMIN")
    assert exc.value.data["code"] == "GOLDEN_RUN_REQUIRED"
    run = _passed_golden_run(db, created["id"])
    run.config_hash = "0" * 64                                          # a run for another configuration
    db.flush()
    with pytest.raises(CampaignException):
        service.promote(created["id"], actor_id=None, actor_role="HR_ADMIN")
    assert db.get(ScoringVersion, run.scoring_version_id).status == STATUS_CANDIDATE

