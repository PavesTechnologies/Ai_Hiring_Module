"""
Audit entries survive the request (real Postgres, over HTTP; like the other
tests/db tests, run against a migrated database).

AuditRepository.create only flushes, and the request session (get_db) is
never committed by the framework - so an audit row is kept only if the code
commits after writing it. Found 2026-10-15: candidate removal (audit written
after the delete's commit) and four XLSX exports (no commit at all) lost every
audit row.

Each test calls the route, then rolls the session back: a row that was only
flushed disappears, a committed one stays. Everything runs in one outer
transaction that is rolled back at the end, so nothing is left in the
database.
"""
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from app.db.session import engine, get_db
from app.enums.constants import API_PREFIX
from app.models.campaigns import HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.compliance import AuditLog
from app.models.pipeline import CampaignCandidate, PipelineStage

_payload = {}


@pytest.fixture
def db():
    connection = engine.connect()
    outer = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        connection.close()


@pytest.fixture
def http(db):
    from app.main import app
    user_id = db.execute(text("select id from users limit 1")).scalar()
    if user_id is None:
        pytest.skip("needs at least one user to act as")
    _payload.clear()
    _payload.update({"user_id": user_id, "email": "u@example.invalid", "roles": ["HR_ADMIN"]})
    with patch("app.middleware.jwt_middleware.decode_token", lambda token: _payload), \
            patch("app.middleware.rbac._ensure_local_user"), \
            patch("app.services.campaign.campaign_candidate_service.CandidateChangeNotifier"):
        app.dependency_overrides[get_db] = lambda: db
        yield TestClient(app, base_url=f"http://test{API_PREFIX}", client=("127.0.0.1", 50000),
                         raise_server_exceptions=True)
    app.dependency_overrides.clear()


_AUTH = {"Authorization": "Bearer t"}


def _audit_ids(db, action_type):
    return set(db.execute(select(AuditLog.id).where(AuditLog.action_type == action_type)).scalars())


def _kept_after_rollback(db, action_type, call):
    """Run call(); roll back what wasn't committed; return the audit rows the call added that remain."""
    before = _audit_ids(db, action_type)
    response = call()
    assert response.status_code == 200, response.text
    db.rollback()
    return _audit_ids(db, action_type) - before


@pytest.fixture
def campaign(db):
    found = db.execute(select(HiringCampaign).limit(1)).scalar_one_or_none()
    if found is None:
        pytest.skip("needs at least one campaign")
    return found


def test_the_ranking_export_keeps_its_audit_entry(db, http, campaign):
    kept = _kept_after_rollback(db, "CANDIDATE_RANKING_EXPORTED",
                                lambda: http.get(f"/campaign-candidates/campaign/{campaign.id}/export", headers=_AUTH))
    assert len(kept) == 1


def test_the_rejected_export_keeps_its_audit_entry(db, http, campaign):
    kept = _kept_after_rollback(
        db, "REJECTED_CANDIDATES_EXPORTED",
        lambda: http.get(f"/campaign-candidates/campaign/{campaign.id}/export-rejected", headers=_AUTH))
    assert len(kept) == 1


def test_the_override_report_export_keeps_its_audit_entry(db, http, campaign):
    kept = _kept_after_rollback(
        db, "OVERRIDE_REPORT_EXPORTED",
        lambda: http.get("/campaign-candidates/override-report/export", params={"campaign_id": str(campaign.id)},
                         headers=_AUTH))
    assert len(kept) == 1


def test_the_rejection_analytics_export_keeps_its_audit_entry(db, http):
    kept = _kept_after_rollback(
        db, "DETERMINISTIC_ANALYTICS_EXPORTED",
        lambda: http.get("/campaign-candidates/rejection-analytics/export", headers=_AUTH))
    assert len(kept) == 1


def _clone(db, source, **overrides):
    mapper = inspect(type(source))
    values = {attr.key: getattr(source, attr.key) for attr in mapper.column_attrs
              if not any(col.primary_key for col in attr.columns) and attr.key not in ("created_at", "updated_at")}
    values.update(overrides)
    row = type(source)(id=uuid4(), **values)
    db.add(row)
    db.flush()
    return row


def test_removing_a_candidate_keeps_its_audit_entry_with_the_delete(db, http, campaign):
    if not inspect(db.connection()).has_table("upload_decisions"):
        # The removal now also closes the candidate's pending upload decisions.
        pytest.skip("needs migration d6f8a0c2e4b7 (upload_decisions) applied")
    source_resume = db.execute(select(Resume).limit(1)).scalar_one_or_none()
    if source_resume is None:
        pytest.skip("needs at least one resume to clone")
    tag = uuid4().hex
    new_campaign = _clone(db, campaign, name=f"zz audit test {tag}")
    candidate = _clone(db, db.get(Candidate, source_resume.candidate_id), email_hash=f"zz-audit-test-{tag}")
    resume = _clone(db, source_resume, candidate_id=candidate.id, file_path=f"zz/audit-test/{tag}.docx",
                    file_hash=tag, bulk_upload_job_id=None)
    cc = CampaignCandidate(id=uuid4(), campaign_id=new_campaign.id, candidate_id=candidate.id, resume_id=resume.id,
                           idempotency_key=f"zz-audit-test-{tag}", pipeline_stage=PipelineStage.UPLOADED,
                           is_fraud_flagged=False)
    db.add(cc)
    db.commit()                 # releases a savepoint only; the outer transaction is rolled back at the end

    kept = _kept_after_rollback(db, "CANDIDATE_REMOVED",
                                lambda: http.delete(f"/campaign-candidates/{cc.id}", headers=_AUTH))

    assert len(kept) == 1
    row = db.get(AuditLog, next(iter(kept)))
    assert row.entity_id == cc.id and row.campaign_id == new_campaign.id
    assert db.get(CampaignCandidate, cc.id) is None           # the delete is kept too
