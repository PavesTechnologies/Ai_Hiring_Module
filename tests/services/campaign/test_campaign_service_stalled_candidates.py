from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.exceptions.campaign_exceptions import CampaignException
from app.services.campaign.campaign_service import CampaignService

"""
Frontend follow-up - candidate_name added to StalledCandidateItem, which
was originally built deliberately PII-free ("anonymous UUID - no PII by
design"). No prior test coverage existed for CampaignService.
get_stalled_candidates at all (confirmed by repo-wide search before this
change) - these are the first tests for this method.
"""

MODULE = "app.services.campaign.campaign_service"


def _row(candidate_id=None, **overrides):
    defaults = dict(
        campaign_candidate_id=uuid4(), candidate_id=candidate_id or uuid4(),
        pipeline_stage="SCREENING", days_stalled=3.0, last_updated_at="2026-08-20T00:00:00Z",
        stall_reason="SCREENING_OVERDUE", last_action_by=None, has_dead_letter_tasks=False,
    )
    defaults.update(overrides)
    return defaults


def _make_service(campaign=None, rows=None, candidates=None):
    campaign_repo = MagicMock()
    campaign_repo.get_by_id.return_value = campaign if campaign is not None else SimpleNamespace(id=uuid4())
    campaign_repo.get_stalled_candidates.return_value = rows or []

    config_repo = MagicMock()
    config_repo.get_configs_by_keys.return_value = {}

    service = CampaignService(
        campaign_repo=campaign_repo, jd_repo=MagicMock(), audit_service=MagicMock(),
        config_repo=config_repo, preset_repo=MagicMock(), db=MagicMock(),
    )

    candidate_repo = MagicMock()
    candidate_repo.get_by_ids.return_value = candidates or []
    return service, campaign_repo, candidate_repo


def test_raises_404_when_campaign_not_found():
    service, campaign_repo, _candidate_repo = _make_service()
    campaign_repo.get_by_id.return_value = None

    with pytest.raises(CampaignException) as exc_info:
        service.get_stalled_candidates(uuid4())

    assert exc_info.value.status_code == 404


def test_includes_decrypted_candidate_name():
    row = _row()
    candidate = SimpleNamespace(id=row["candidate_id"], full_name_encrypted=b"enc(Jordan Lee)", encryption_key_id=uuid4())
    service, _campaign_repo, candidate_repo = _make_service(rows=[row], candidates=[candidate])

    with patch(f"{MODULE}.CandidateRepository", return_value=candidate_repo), \
         patch(f"{MODULE}.EncryptionService") as mock_encryption_cls:
        mock_encryption_cls.return_value.decrypt.return_value = "Jordan Lee"
        result = service.get_stalled_candidates(uuid4())

    assert len(result.items) == 1
    assert result.items[0].candidate_name == "Jordan Lee"
    mock_encryption_cls.return_value.decrypt.assert_called_once_with(
        candidate.full_name_encrypted, candidate.encryption_key_id,
    )


def test_falls_back_to_unknown_when_candidate_row_missing():
    row = _row()
    service, _campaign_repo, candidate_repo = _make_service(rows=[row], candidates=[])

    with patch(f"{MODULE}.CandidateRepository", return_value=candidate_repo), \
         patch(f"{MODULE}.EncryptionService") as mock_encryption_cls:
        result = service.get_stalled_candidates(uuid4())

    assert result.items[0].candidate_name == "Unknown"
    mock_encryption_cls.return_value.decrypt.assert_not_called()


def test_no_regression_to_existing_fields():
    row = _row(
        pipeline_stage="HM_REVIEW", days_stalled=6.5, stall_reason="HM_REVIEW_OVERDUE",
        last_action_by="hm-1", has_dead_letter_tasks=True,
    )
    candidate = SimpleNamespace(id=row["candidate_id"], full_name_encrypted=b"enc", encryption_key_id=uuid4())
    service, _campaign_repo, candidate_repo = _make_service(rows=[row], candidates=[candidate])

    with patch(f"{MODULE}.CandidateRepository", return_value=candidate_repo), \
         patch(f"{MODULE}.EncryptionService") as mock_encryption_cls:
        mock_encryption_cls.return_value.decrypt.return_value = "Someone"
        result = service.get_stalled_candidates(uuid4())

    item = result.items[0]
    assert item.campaign_candidate_id == row["campaign_candidate_id"]
    assert item.pipeline_stage == "HM_REVIEW"
    assert item.days_stalled == 6.5
    assert item.stall_reason == "HM_REVIEW_OVERDUE"
    assert item.last_action_by == "hm-1"
    assert item.has_dead_letter_tasks is True
    assert result.total == 1
    assert "screening_sla_hours" in result.sla_config


def test_empty_result_is_a_clean_no_op():
    """get_by_ids([]) is called but short-circuits internally (its own guard) - no real query, no decrypt call."""
    service, _campaign_repo, candidate_repo = _make_service(rows=[])

    with patch(f"{MODULE}.CandidateRepository", return_value=candidate_repo), \
         patch(f"{MODULE}.EncryptionService") as mock_encryption_cls:
        result = service.get_stalled_candidates(uuid4())

    assert result.items == []
    assert result.total == 0
    mock_encryption_cls.return_value.decrypt.assert_not_called()


def test_multiple_stalled_candidates_each_get_their_own_name():
    row_a = _row()
    row_b = _row()
    candidate_a = SimpleNamespace(id=row_a["candidate_id"], full_name_encrypted=b"enc-a", encryption_key_id=uuid4())
    candidate_b = SimpleNamespace(id=row_b["candidate_id"], full_name_encrypted=b"enc-b", encryption_key_id=uuid4())
    service, _campaign_repo, candidate_repo = _make_service(rows=[row_a, row_b], candidates=[candidate_a, candidate_b])

    with patch(f"{MODULE}.CandidateRepository", return_value=candidate_repo), \
         patch(f"{MODULE}.EncryptionService") as mock_encryption_cls:
        mock_encryption_cls.return_value.decrypt.side_effect = ["Alice", "Bob"]
        result = service.get_stalled_candidates(uuid4())

    names = {item.campaign_candidate_id: item.candidate_name for item in result.items}
    assert names[row_a["campaign_candidate_id"]] == "Alice"
    assert names[row_b["campaign_candidate_id"]] == "Bob"
