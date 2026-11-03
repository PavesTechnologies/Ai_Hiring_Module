"""Scoring versions (retire v1, phase D): configuration and model per version, try / promote / retire."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.models.scoring_v2 import ScoringVersion
from app.services.campaign import scoring_versions as sv
from app.services.campaign.scoring_v2 import DEFAULT_CONFIG


def _version(status=sv.STATUS_CANDIDATE, **kw):
    return ScoringVersion(id=uuid4(), label=kw.pop("label", f"v-{status}"), status=status,
                          configuration=kw.pop("configuration", {}), model=kw.pop("model", None), **kw)


def _service(db=None):
    return sv.ScoringVersionService(db or MagicMock(), MagicMock())


def test_a_versions_configuration_is_merged_over_the_defaults():
    version = _version(configuration={"component_weights": {"skills": 0.5}})
    config = sv.version_config(version)
    assert config["component_weights"]["skills"] == 0.5
    assert set(config) == set(DEFAULT_CONFIG)                       # nothing dropped
    assert sv.version_config(_version(configuration=None)) == sv.version_config(_version())


def test_unknown_configuration_keys_are_refused():
    with pytest.raises(CampaignException) as exc:
        sv.validate_configuration({"component_weight": {}})           # typo of component_weights
    assert exc.value.status_code == 422 and exc.value.data["unknown_keys"] == ["component_weight"]
    with pytest.raises(CampaignException):
        sv.validate_configuration(["not", "an", "object"])


def test_current_version_ignores_anything_that_is_not_a_version():
    db = MagicMock()                                                  # a mocked session returns mocks
    assert sv.current_version(db) is None
    row = _version(sv.STATUS_CURRENT)
    db.execute.return_value.scalars.return_value.first.return_value = row
    assert sv.current_version(db) is row


def test_a_version_with_a_model_votes_with_that_model_only():
    default = MagicMock(name="platform resolver")
    assert sv.provider_resolver_for(MagicMock(), _version(), default) is default
    assert sv.provider_resolver_for(MagicMock(), None, default) is default
    resolver = sv.provider_resolver_for(MagicMock(), _version(model="gemini-3.5-flash"), default)
    with patch("app.repositories.ai_provider_config_repository.AIProviderConfigRepository") as repo, \
            patch("app.services.llm.factory.resolve_provider_model", return_value="provider") as build:
        repo.return_value.get_active.return_value = SimpleNamespace(provider="GOOGLE")
        assert resolver() == "provider"
    assert build.call_args.args[1:] == ("GOOGLE", "gemini-3.5-flash")


def test_promote_retires_the_current_version_and_stops_campaigns_trying_it():
    old, new = _version(sv.STATUS_CURRENT, label="old"), _version(label="new")
    trying = SimpleNamespace(candidate_scoring_version_id=new.id)
    db = MagicMock()
    db.get.return_value = new
    db.execute.return_value.scalars.return_value.first.return_value = old
    db.execute.return_value.scalars.return_value.all.return_value = [trying]
    service = _service(db)
    result = service.promote(new.id, actor_id="u1", actor_role="HR_ADMIN")
    assert (old.status, new.status, new.activated_by) == (sv.STATUS_RETIRED, sv.STATUS_CURRENT, "u1")
    assert old.retired_at is not None and new.activated_at is not None
    assert trying.candidate_scoring_version_id is None
    details = service.audit_service.log.call_args.kwargs["details"]
    assert (details["change"], details["previous_current_label"], details["campaigns_stopped_trying"]) == (
        "promoted", "old", 1)
    assert result["status"] == sv.STATUS_CURRENT
    db.commit.assert_called_once()


@pytest.mark.parametrize("status", [sv.STATUS_CURRENT, sv.STATUS_RETIRED])
def test_only_a_candidate_version_can_be_promoted_retired_or_tried(status):
    version = _version(status)
    db = MagicMock()
    db.get.return_value = version
    service = _service(db)
    for call in (lambda: service.promote(version.id, actor_id="u1", actor_role="HR_ADMIN"),
                 lambda: service.retire(version.id, actor_id="u1", actor_role="HR_ADMIN")):
        with pytest.raises(CampaignException) as exc:
            call()
        assert exc.value.status_code == 409
    with patch("app.repositories.CampaignRepository.CampaignRepository") as repo:
        repo.return_value.get_by_id.return_value = SimpleNamespace(id=uuid4(), candidate_scoring_version_id=None)
        with pytest.raises(CampaignException):
            service.set_campaign_candidate(uuid4(), version_id=version.id, backfill=False, actor_id="u1",
                                           actor_role="HR_ADMIN")
    db.commit.assert_not_called()


def test_trying_a_version_on_a_campaign_is_audited_and_can_be_stopped():
    version = _version(label="weights 0.6")
    campaign = SimpleNamespace(id=uuid4(), candidate_scoring_version_id=None)
    db = MagicMock()
    db.get.return_value = version
    service = _service(db)
    with patch("app.repositories.CampaignRepository.CampaignRepository") as repo:
        repo.return_value.get_by_id.return_value = campaign
        result = service.set_campaign_candidate(campaign.id, version_id=version.id, backfill=False,
                                                actor_id="u1", actor_role="HR_ADMIN")
        assert campaign.candidate_scoring_version_id == version.id and result["queued"] == 0
        assert service.audit_service.log.call_args.kwargs["details"]["candidate_scoring_version_label"] == "weights 0.6"
        service.set_campaign_candidate(campaign.id, version_id=None, backfill=True, actor_id="u1", actor_role="HR_ADMIN")
    assert campaign.candidate_scoring_version_id is None
    assert service.audit_service.log.call_args.kwargs["details"]["previous_candidate_scoring_version_id"] == str(version.id)


def test_create_refuses_a_duplicate_label():
    db = MagicMock()
    db.execute.return_value.first.return_value = (uuid4(),)
    with pytest.raises(CampaignException) as exc:
        _service(db).create(label="v2 baseline", configuration={}, model=None, note=None, actor_id="u1",
                            actor_role="HR_ADMIN")
    assert exc.value.status_code == 409


def test_scoring_uses_the_current_version_unless_given_one_and_restores_the_resolver():
    from app.services.campaign.scoring_v2_service import ScoringV2Service
    current, tried = _version(sv.STATUS_CURRENT), _version(model="gemini-3.5-flash")
    platform = MagicMock(name="platform resolver")
    service = ScoringV2Service.__new__(ScoringV2Service)
    service.db, service.provider_resolver = MagicMock(), platform
    seen = []

    def fake_score(**kwargs):
        seen.append((kwargs["version"], service.provider_resolver))
        return {}
    service._score = fake_score
    args = dict(campaign=SimpleNamespace(id=uuid4()), campaign_candidate=None, job_description=None,
                resume=SimpleNamespace(id=uuid4()), experience_result=None, education_result=None,
                domain_result=None, acted_on=True)
    with patch.object(sv, "current_version", return_value=current):
        service.score(**args)
        service.score(**{**args, "acted_on": False}, scoring_version=tried)
    assert seen[0] == (current, platform)                            # no model: the platform's resolver
    assert seen[1][0] is tried and seen[1][1] is not platform         # its own model's resolver
    assert service.provider_resolver is platform                      # restored after each call
