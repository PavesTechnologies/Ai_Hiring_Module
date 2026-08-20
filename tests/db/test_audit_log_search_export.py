"""
Epic 3 Fix 5: real-DB coverage for AuditRepository.search/iter_for_export -
actual filtering, pagination, ordering, streaming/batching behavior, and
detail JSON round-tripping can't be verified through a mock (same
reasoning as tests/db/test_audit_log_immutability.py, the first real-DB
test file in this codebase). Every test here inserts rows inside a
transaction that is rolled back in fixture teardown - nothing is ever
left behind, and no pre-existing row is touched.

actor_id/campaign_id are real FKs (users.id/hiring_campaigns.id) - test
rows reuse real existing ids fetched at fixture setup rather than
inventing ones that would violate the constraint. entity_id has no FK, so
fresh UUIDs are used freely there.

Isolation note: real_ids["users"]/["campaigns"] are pre-existing, actively
used rows in a shared dev table - they may already have unrelated real
audit_log rows referencing them (confirmed true for at least one campaign
id, which already had 50+ rows from Fix 3's own EXPLAIN ANALYZE testing).
Filters keyed on those real, shared ids therefore assert "my expected rows
are present" (subset checks), never an exact total/set - only entity_id
(freshly generated UUIDs, zero collision risk) is used where an exact
count or exact ordered list is asserted.
"""
import csv as csv_module
import io
import json as json_module
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.db.session import SessionLocal
from app.enums.constants import ActionType, EntityType
from app.models.compliance import AuditLog
from app.repositories.audit_repository import AuditRepository
from app.repositories.CampaignRepository import CampaignRepository
from app.services.audit_service import AuditService


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def real_ids(db):
    user_ids = [r[0] for r in db.execute(text("SELECT id FROM users LIMIT 2")).fetchall()]
    campaign_ids = [r[0] for r in db.execute(text("SELECT id FROM hiring_campaigns LIMIT 2")).fetchall()]
    if len(user_ids) < 2 or len(campaign_ids) < 2:
        pytest.skip("Fixture requires at least 2 existing users and 2 existing campaigns in the target DB.")
    return {"users": user_ids, "campaigns": campaign_ids}


@pytest.fixture
def seeded_rows(db, real_ids):
    """
    4 rows spanning distinct actor/campaign/entity/action_type/time
    combinations:
      A: user[0], campaign[0], CANDIDATE/entity_x, CANDIDATE_ADDED, t-3d
      B: SYSTEM (actor_id=None), campaign[0], CAMPAIGN_CANDIDATE/entity_y, PIPELINE_STAGE_TRANSITIONED, t-2d
      C: user[1], campaign[1], CAMPAIGN/entity_z, CAMPAIGN_CREATED, t-1d
      D: user[0], campaign[1], CANDIDATE/entity_x (same as A), CANDIDATE_ADDED, t-0d
    entity_x/y/z are fresh UUIDs - the only values here guaranteed not to
    collide with any pre-existing row.
    """
    now = datetime.now(timezone.utc)
    entity_x, entity_y, entity_z = uuid4(), uuid4(), uuid4()

    rows = [
        AuditLog(
            id=uuid4(), actor_id=real_ids["users"][0], actor_role="HR_ADMIN",
            action_type=ActionType.CANDIDATE_ADDED, entity_type=EntityType.CANDIDATE, entity_id=entity_x,
            campaign_id=real_ids["campaigns"][0], detail={"row": "A"}, created_at=now - timedelta(days=3),
        ),
        AuditLog(
            id=uuid4(), actor_id=None, actor_role="SYSTEM",
            action_type=ActionType.PIPELINE_STAGE_TRANSITIONED, entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=entity_y,
            campaign_id=real_ids["campaigns"][0], detail={"row": "B", "nested": {"from": "SCREENING", "to": "SHORTLISTED"}},
            created_at=now - timedelta(days=2),
        ),
        AuditLog(
            id=uuid4(), actor_id=real_ids["users"][1], actor_role="HR_ADMIN",
            action_type=ActionType.CAMPAIGN_CREATED, entity_type=EntityType.CAMPAIGN, entity_id=entity_z,
            campaign_id=real_ids["campaigns"][1], detail={"row": "C", "list": [1, 2, 3]}, created_at=now - timedelta(days=1),
        ),
        AuditLog(
            id=uuid4(), actor_id=real_ids["users"][0], actor_role="HR_ADMIN",
            action_type=ActionType.CANDIDATE_ADDED, entity_type=EntityType.CANDIDATE, entity_id=entity_x,
            campaign_id=real_ids["campaigns"][1], detail=None, created_at=now,
        ),
    ]
    for row in rows:
        db.add(row)
    db.flush()

    return {
        "A": rows[0], "B": rows[1], "C": rows[2], "D": rows[3],
        "entity_x": entity_x, "entity_y": entity_y, "entity_z": entity_z,
        "now": now,
    }


def _ids(rows):
    return {row.id for row in rows}


# ----------------------------------------------------------------------
# search() - individual filters. entity_id-anchored checks assert exact
# sets (safe - fresh, collision-free UUIDs); real/shared-id checks assert
# only that the expected rows are present (safe regardless of whatever
# else already exists for that real user/campaign/action_type).
# ----------------------------------------------------------------------

def test_search_filters_by_entity_type_and_entity_id(db, real_ids, seeded_rows):
    repo = AuditRepository(db)

    rows, total = repo.search(entity_type=EntityType.CANDIDATE.value, entity_id=seeded_rows["entity_x"], page_size=50)

    assert total == 2
    assert _ids(rows) == {seeded_rows["A"].id, seeded_rows["D"].id}


def test_search_filters_by_actor_id(db, real_ids, seeded_rows):
    repo = AuditRepository(db)

    rows, total = repo.search(actor_id=real_ids["users"][0], page_size=50)

    assert total >= 2
    assert {seeded_rows["A"].id, seeded_rows["D"].id} <= _ids(rows)


def test_search_filters_by_campaign_id(db, real_ids, seeded_rows):
    repo = AuditRepository(db)

    rows, total = repo.search(campaign_id=real_ids["campaigns"][0], page_size=200)

    assert total >= 2
    assert {seeded_rows["A"].id, seeded_rows["B"].id} <= _ids(rows)


def test_search_filters_by_action_type(db, real_ids, seeded_rows):
    repo = AuditRepository(db)

    rows, total = repo.search(action_type=ActionType.CANDIDATE_ADDED.value, entity_id=seeded_rows["entity_x"], page_size=50)

    # entity_id narrows to exactly this fixture's 2 CANDIDATE_ADDED rows,
    # even though CANDIDATE_ADDED alone would match many unrelated real rows.
    assert total == 2
    assert _ids(rows) == {seeded_rows["A"].id, seeded_rows["D"].id}


def test_search_filters_by_created_at_range(db, real_ids, seeded_rows):
    """
    entity_id=entity_x isolates to exactly {A, D} regardless of anything
    else in the table; the created_at range then further excludes A
    (t-3d), leaving only D (t-0) - proves the range filter itself works,
    on top of an already-isolated base set.
    """
    repo = AuditRepository(db)
    now = seeded_rows["now"]

    rows, total = repo.search(
        entity_id=seeded_rows["entity_x"],
        created_from=now - timedelta(days=1),
        created_to=now + timedelta(minutes=1),
        page_size=50,
    )

    assert total == 1
    assert _ids(rows) == {seeded_rows["D"].id}


def test_search_combines_filters_with_and(db, real_ids, seeded_rows):
    repo = AuditRepository(db)

    # entity_id already isolates to {A, D} - adding actor_id must not
    # change that (both A and D share real_ids["users"][0]).
    rows, total = repo.search(
        entity_id=seeded_rows["entity_x"], actor_id=real_ids["users"][0], page_size=50,
    )
    assert total == 2
    assert _ids(rows) == {seeded_rows["A"].id, seeded_rows["D"].id}

    # Narrow further with campaign_id - only D matches all three (D's
    # campaign_id is campaigns[1]; A's is campaigns[0]).
    rows, total = repo.search(
        entity_id=seeded_rows["entity_x"], actor_id=real_ids["users"][0],
        campaign_id=real_ids["campaigns"][1], page_size=50,
    )
    assert total == 1
    assert _ids(rows) == {seeded_rows["D"].id}


def test_search_pagination_returns_correct_page_and_total(db, real_ids, seeded_rows):
    """
    entity_id=entity_x deterministically isolates to exactly {A, D} -
    paginating an unfiltered or loosely-filtered query would be unreliable
    in a shared dev table (other concurrent rows could rank ahead of the
    seeded ones).
    """
    repo = AuditRepository(db)

    page1, total1 = repo.search(entity_id=seeded_rows["entity_x"], page=1, page_size=1)
    page2, total2 = repo.search(entity_id=seeded_rows["entity_x"], page=2, page_size=1)

    assert total1 == total2 == 2
    assert len(page1) == 1 and len(page2) == 1
    assert page1[0].id == seeded_rows["D"].id  # most recent first (created_at DESC)
    assert page2[0].id == seeded_rows["A"].id
    assert _ids(page1) | _ids(page2) == {seeded_rows["A"].id, seeded_rows["D"].id}


def test_search_default_sort_is_created_at_desc(db, real_ids, seeded_rows):
    repo = AuditRepository(db)

    rows, _ = repo.search(entity_id=seeded_rows["entity_x"], page_size=50)

    assert [r.id for r in rows] == [seeded_rows["D"].id, seeded_rows["A"].id]
    timestamps = [r.created_at for r in rows]
    assert timestamps == sorted(timestamps, reverse=True)


# ----------------------------------------------------------------------
# export vs. search - identical filtered set; streaming/batching; detail
# JSON round-trip.
# ----------------------------------------------------------------------

def test_export_produces_the_same_filtered_set_as_search(db, real_ids, seeded_rows):
    """
    A relative comparison (search's ids == export's ids for the identical
    filter) - safe regardless of how many other real rows also match
    campaign_id, since both queries see the same table state. page_size is
    derived from the real total (capped at MAX_PAGE_SIZE, fetched across
    as many pages as needed) rather than a guessed constant, so this holds
    however many pre-existing rows that real campaign_id already has.
    """
    from app.enums.constants import MAX_PAGE_SIZE

    repo = AuditRepository(db)

    _, total = repo.search(campaign_id=real_ids["campaigns"][0], page=1, page_size=1)
    search_ids = set()
    page = 1
    while len(search_ids) < total:
        rows, _ = repo.search(campaign_id=real_ids["campaigns"][0], page=page, page_size=MAX_PAGE_SIZE)
        search_ids |= _ids(rows)
        page += 1

    export_rows = list(repo.iter_for_export(campaign_id=real_ids["campaigns"][0]))

    assert search_ids == _ids(export_rows)
    assert {seeded_rows["A"].id, seeded_rows["B"].id} <= _ids(export_rows)


def test_export_via_service_produces_same_ids_as_search_via_service(db, real_ids, seeded_rows):
    """
    Comparing search's page against export's full stream is only valid
    when the total fits in one page - a real/shared campaign_id has an
    unknown, potentially-unbounded row count (search would paginate,
    export wouldn't, and the sets would legitimately differ for reasons
    unrelated to this test). entity_id=entity_x isolates to exactly 2 rows,
    safely within any page_size, so this compares what it means to compare.
    """
    audit_repo = AuditRepository(db)
    campaign_repo = CampaignRepository(db)
    service = AuditService(repository=audit_repo, campaign_repo=campaign_repo)

    search_result = service.search_audit_log(entity_id=seeded_rows["entity_x"], page_size=50)
    csv_text = "".join(service.export_audit_log_csv(entity_id=seeded_rows["entity_x"]))

    exported_ids = {row["id"] for row in csv_module.DictReader(io.StringIO(csv_text))}
    searched_ids = {str(item.id) for item in search_result.items}
    assert exported_ids == searched_ids == {str(seeded_rows["A"].id), str(seeded_rows["D"].id)}


def test_iter_for_export_batches_instead_of_one_unbounded_query(db, real_ids, seeded_rows, monkeypatch):
    """
    entity_id=entity_x isolates to exactly {A, D} (2 rows) - with
    batch_size=1, fetching both requires at least 2 round trips (a 3rd,
    empty one confirms exhaustion), never a single unbounded query.
    """
    repo = AuditRepository(db)
    real_execute = db.execute
    call_count = 0

    def _counting_execute(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_execute(*args, **kwargs)

    monkeypatch.setattr(db, "execute", _counting_execute)

    rows = list(repo.iter_for_export(entity_id=seeded_rows["entity_x"], batch_size=1))

    assert _ids(rows) == {seeded_rows["A"].id, seeded_rows["D"].id}
    assert call_count >= 2  # at least 2 batches fetched, not 1 unbounded SELECT


def test_export_detail_json_round_trips_for_varied_shapes(db, real_ids, seeded_rows):
    audit_repo = AuditRepository(db)
    service = AuditService(repository=audit_repo, campaign_repo=CampaignRepository(db))

    # A: plain flat dict.
    csv_text_a = "".join(service.export_audit_log_csv(entity_id=seeded_rows["entity_x"]))
    by_id = {row["id"]: row for row in csv_module.DictReader(io.StringIO(csv_text_a))}
    assert json_module.loads(by_id[str(seeded_rows["A"].id)]["detail"]) == {"row": "A"}
    # D: detail=None -> empty string, not the literal "null".
    assert by_id[str(seeded_rows["D"].id)]["detail"] == ""

    # B: nested dict, isolated via entity_id (avoids picking up any other
    # real CAMPAIGN_CANDIDATE-entity-type row that could otherwise sort
    # ahead of it).
    csv_text_b = "".join(service.export_audit_log_csv(entity_id=seeded_rows["entity_y"]))
    row_b_csv = next(csv_module.DictReader(io.StringIO(csv_text_b)))
    assert json_module.loads(row_b_csv["detail"]) == {"row": "B", "nested": {"from": "SCREENING", "to": "SHORTLISTED"}}

    # C: dict containing a list value.
    csv_text_c = "".join(service.export_audit_log_csv(entity_id=seeded_rows["entity_z"]))
    row_c_csv = next(csv_module.DictReader(io.StringIO(csv_text_c)))
    assert json_module.loads(row_c_csv["detail"]) == {"row": "C", "list": [1, 2, 3]}
