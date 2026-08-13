"""
Epic 3 Fix 5: AuditService.search_audit_log/export_audit_log_csv, unit
tested against a mocked AuditRepository/CampaignRepository. The real-DB
behaviors that can't be verified through a mock (actual filtering,
streaming/batching, detail JSON round-tripping against real Postgres) are
covered separately in tests/db/test_audit_log_search_export.py.
"""
import csv
import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.enums.constants import ActionType, EntityType, MAX_PAGE_SIZE
from app.exceptions.campaign_exceptions import CampaignException
from app.schemas.audit.audit_log_schema import AuditLogSearchResponse
from app.services.audit_service import AuditService


def _make_row(**overrides):
    defaults = dict(
        id=uuid4(),
        actor_id="hr-1",
        actor_role="HR_ADMIN",
        action_type=ActionType.CAMPAIGN_CREATED,
        entity_type=EntityType.CAMPAIGN,
        entity_id=uuid4(),
        campaign_id=uuid4(),
        detail={"title": "x"},
        created_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_service(rows=None, total=0, campaign_repo=None):
    repository = MagicMock()
    repository.search.return_value = (rows or [], total)
    repository.iter_for_export.return_value = iter(rows or [])
    campaign_repo = campaign_repo if campaign_repo is not None else MagicMock()
    service = AuditService(repository=repository, campaign_repo=campaign_repo)
    return service, repository, campaign_repo


# ----------------------------------------------------------------------
# search_audit_log - filter pass-through, validation, actor resolution.
# ----------------------------------------------------------------------

def test_search_audit_log_passes_every_filter_through_to_the_repository():
    service, repository, campaign_repo = _make_service()
    campaign_repo.get_user_names.return_value = {}
    entity_id = uuid4()
    campaign_id = uuid4()

    service.search_audit_log(
        entity_type=EntityType.CAMPAIGN_CANDIDATE,
        entity_id=entity_id,
        actor_id="hr-1",
        campaign_id=campaign_id,
        action_type=ActionType.PIPELINE_STAGE_TRANSITIONED,
        created_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_to=datetime(2026, 12, 31, tzinfo=timezone.utc),
        page=2,
        page_size=10,
    )

    repository.search.assert_called_once_with(
        entity_type="CAMPAIGN_CANDIDATE",
        entity_id=entity_id,
        actor_id="hr-1",
        campaign_id=campaign_id,
        action_type="PIPELINE_STAGE_TRANSITIONED",
        created_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_to=datetime(2026, 12, 31, tzinfo=timezone.utc),
        page=2,
        page_size=10,
    )


def test_search_audit_log_page_below_one_raises_422():
    service, _, _ = _make_service()

    with pytest.raises(CampaignException) as exc_info:
        service.search_audit_log(page=0, page_size=20)

    assert exc_info.value.status_code == 422


def test_search_audit_log_page_size_out_of_range_raises_422():
    service, _, _ = _make_service()

    with pytest.raises(CampaignException) as exc_info:
        service.search_audit_log(page=1, page_size=MAX_PAGE_SIZE + 1)

    assert exc_info.value.status_code == 422


def test_search_audit_log_created_from_after_created_to_raises_422():
    service, _, _ = _make_service()

    with pytest.raises(CampaignException) as exc_info:
        service.search_audit_log(
            created_from=datetime(2026, 12, 31, tzinfo=timezone.utc),
            created_to=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

    assert exc_info.value.status_code == 422


def test_search_audit_log_resolves_actor_names_via_campaign_repo():
    row = _make_row(actor_id="hr-1")
    service, repository, campaign_repo = _make_service(rows=[row], total=1)
    campaign_repo.get_user_names.return_value = {"hr-1": "Jordan HR"}

    result = service.search_audit_log()

    assert isinstance(result, AuditLogSearchResponse)
    campaign_repo.get_user_names.assert_called_once_with(["hr-1"])
    assert result.items[0].actor_name == "Jordan HR"
    assert result.total == 1


def test_search_audit_log_unresolved_or_null_actor_falls_back_to_system():
    rows = [_make_row(actor_id=None), _make_row(actor_id="ghost-user")]
    service, repository, campaign_repo = _make_service(rows=rows, total=2)
    campaign_repo.get_user_names.return_value = {}

    result = service.search_audit_log()

    assert result.items[0].actor_name == "System"
    assert result.items[1].actor_name == "System"


def test_search_audit_log_works_without_a_campaign_repo():
    row = _make_row(actor_id="hr-1")
    repository = MagicMock()
    repository.search.return_value = ([row], 1)
    service = AuditService(repository=repository, campaign_repo=None)

    result = service.search_audit_log()

    assert result.items[0].actor_name == "System"


# ----------------------------------------------------------------------
# export_audit_log_csv - same filters as search, streaming CSV output.
# ----------------------------------------------------------------------

def test_export_audit_log_csv_uses_the_same_filters_as_search():
    service, repository, campaign_repo = _make_service()
    campaign_repo.get_user_names.return_value = {}
    entity_id = uuid4()
    campaign_id = uuid4()

    search_kwargs = dict(
        entity_type=EntityType.RESUME,
        entity_id=entity_id,
        actor_id="hr-1",
        campaign_id=campaign_id,
        action_type=ActionType.RESUME_UPLOADED,
        created_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
        created_to=datetime(2026, 12, 31, tzinfo=timezone.utc),
    )

    service.search_audit_log(**search_kwargs, page=1, page_size=20)
    list(service.export_audit_log_csv(**search_kwargs))

    search_call = repository.search.call_args.kwargs
    export_call = repository.iter_for_export.call_args.kwargs
    shared_keys = ["entity_type", "entity_id", "actor_id", "campaign_id", "action_type", "created_from", "created_to"]
    assert {k: search_call[k] for k in shared_keys} == {k: export_call[k] for k in shared_keys}


def test_export_audit_log_csv_streams_header_then_one_row_per_yield():
    row = _make_row(
        actor_id="hr-1",
        action_type=ActionType.CAMPAIGN_CREATED,
        entity_type=EntityType.CAMPAIGN,
        detail={"nested": {"a": 1}, "list": [1, 2, 3]},
    )
    service, repository, campaign_repo = _make_service(rows=[row])
    campaign_repo.get_user_names.return_value = {"hr-1": "Jordan HR"}

    chunks = list(service.export_audit_log_csv())

    assert len(chunks) == 2  # header chunk, then exactly one row chunk
    full_csv = "".join(chunks)
    reader = csv.DictReader(io.StringIO(full_csv))
    parsed_rows = list(reader)
    assert len(parsed_rows) == 1
    assert parsed_rows[0]["actor_id"] == "hr-1"
    assert parsed_rows[0]["actor_name"] == "Jordan HR"
    assert parsed_rows[0]["action_type"] == "CAMPAIGN_CREATED"
    assert parsed_rows[0]["entity_type"] == "CAMPAIGN"
    assert json.loads(parsed_rows[0]["detail"]) == {"nested": {"a": 1}, "list": [1, 2, 3]}


def test_export_audit_log_csv_never_materializes_more_than_one_row_at_a_time():
    """
    iter_for_export is a generator on the repository side (real batching is
    tested against a real DB in tests/db/) - this confirms the SERVICE
    layer's generator consumes it lazily (one yield in, one yield out),
    not by exhausting it into a list before writing anything.
    """
    call_count = 0

    def _rows():
        nonlocal call_count
        for i in range(3):
            call_count += 1
            yield _make_row(actor_id=None)

    repository = MagicMock()
    repository.iter_for_export.return_value = _rows()
    service = AuditService(repository=repository, campaign_repo=None)

    gen = service.export_audit_log_csv()
    next(gen)  # header
    assert call_count == 0  # nothing pulled from iter_for_export yet

    next(gen)  # first row
    assert call_count == 1  # exactly one row pulled, not all 3
