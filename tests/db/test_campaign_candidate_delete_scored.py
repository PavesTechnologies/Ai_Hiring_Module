"""
Removing a scored candidate from a campaign, and erasing a scored candidate,
against real Postgres (like the other tests/db tests, run against a migrated
database).

Every table that references campaign_candidates / resumes has an FK with no
ON DELETE rule, so a missed table only shows up as a ForeignKeyViolation on
the real database - which mocks can't catch. Seen live: removing a candidate
scored in v1 and v2 failed on candidate_scoring_results.

The rows are new (cloned from an existing campaign / candidate / resume for
their required fields) and everything runs in one outer transaction that is
rolled back; the services' own commits only release a savepoint.
"""
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models.campaigns import HiringCampaign
from app.models.candidates import Candidate, Resume
from app.models.outcome_overrides import CandidateOutcomeOverride
from app.models.pipeline import CampaignCandidate, CampaignCandidateStageHistory, PipelineStage, TransitionSource
from app.models.scoring_v2 import (
    CandidateScoringResult,
    CoreSkillConfirmation,
    ResumeParseVersion,
    ResumeProvisionalSkillScan,
    ResumeSkillEvidence,
    ScoringShadowRanking,
)
from app.models.skills import SkillOntology
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.services.campaign.campaign_candidate_service import CampaignCandidateService


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


def _clone(db, source, **overrides):
    """A new row with the source row's column values (new primary key), plus overrides."""
    mapper = inspect(type(source))
    values = {attr.key: getattr(source, attr.key) for attr in mapper.column_attrs
              if not any(col.primary_key for col in attr.columns)
              and attr.key not in ("created_at", "updated_at")}
    values.update(overrides)
    row = type(source)(id=uuid4(), **values)
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def scored(db):
    """A new campaign with one candidate scored in v1 and v2, a shadow-report rank and v2 resume data."""
    source_campaign = db.execute(select(HiringCampaign).limit(1)).scalar_one_or_none()
    source_resume = db.execute(select(Resume).limit(1)).scalar_one_or_none()
    skill = db.execute(select(SkillOntology).limit(1)).scalar_one_or_none()
    if source_campaign is None or source_resume is None or skill is None:
        pytest.skip("needs at least one campaign, resume and catalogue skill to clone from")
    source_candidate = db.get(Candidate, source_resume.candidate_id)

    tag = uuid4().hex
    campaign = _clone(db, source_campaign, name=f"zz delete test {tag}")
    candidate = _clone(db, source_candidate, email_hash=f"zz-delete-test-{tag}")
    resume = _clone(db, source_resume, candidate_id=candidate.id, file_path=f"zz/delete-test/{tag}.docx",
                    file_hash=tag, bulk_upload_job_id=None)
    cc = CampaignCandidate(id=uuid4(), campaign_id=campaign.id, candidate_id=candidate.id, resume_id=resume.id,
                           idempotency_key=f"zz-delete-test-{tag}", pipeline_stage=PipelineStage.UPLOADED,
                           is_fraud_flagged=False)
    db.add(cc)
    db.flush()
    db.add_all([
        CampaignCandidateStageHistory(campaign_candidate_id=cc.id, to_stage=PipelineStage.UPLOADED,
                                      transition_source=TransitionSource.SYSTEM),
        CandidateScoringResult(campaign_candidate_id=cc.id, scoring_version="v1", scoring_config_version="t",
                               acted_on=True),
        CandidateScoringResult(campaign_candidate_id=cc.id, scoring_version="v2", scoring_config_version="t"),
        ScoringShadowRanking(campaign_id=campaign.id, campaign_candidate_id=cc.id, recruiter_rank=1,
                             ranked_by_name="Test"),
        ResumeSkillEvidence(resume_id=resume.id, canonical_skill_id=skill.id, sources=["SKILLS"],
                            best_source="SKILLS", match_tier="EXACT", recognition=1, text_hash=tag,
                            detection_version="t"),
        CoreSkillConfirmation(cache_key=tag, resume_id=resume.id, jd_id=campaign.jd_id, text_hash=tag,
                              core_set_hash=tag, prompt_version="t", detection_version="t"),
        ResumeParseVersion(resume_id=resume.id, version_no=1),
        ResumeProvisionalSkillScan(resume_id=resume.id, text_hash=tag, detection_version="t"),
    ])
    db.flush()
    # A recorded override references the result it contradicted - removal and erasure must take it too.
    acted_on = db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc.id,
                                                              CandidateScoringResult.acted_on.is_(True))).scalar_one()
    db.add(CandidateOutcomeOverride(campaign_candidate_id=cc.id, campaign_id=campaign.id, scoring_result_id=acted_on.id,
                                    system_outcome="REJECT", kind="OVERRIDE", direction="UP",
                                    from_stage=PipelineStage.REJECTED, to_stage=PipelineStage.SCREENING,
                                    reason_code="SKILL_EVIDENCE_MISSED"))
    db.flush()
    return dict(campaign=campaign, candidate=candidate, resume=resume, cc=cc)


def _count(db, model, **where):
    query = select(func.count()).select_from(model)
    for column, value in where.items():
        query = query.where(getattr(model, column) == value)
    return db.execute(query).scalar()


def test_removing_a_scored_candidate_with_a_shadow_rank_from_a_campaign(db, scored):
    cc = scored["cc"]
    service = CampaignCandidateService(campaign_repo=MagicMock(), campaign_candidate_repo=CampaignCandidateRepository(db),
                                       audit_service=MagicMock())

    with patch("app.services.campaign.campaign_candidate_service.CandidateChangeNotifier"):
        service.delete_campaign_candidate(cc.id, actor_id="hr-1", actor_role="HR_ADMIN")

    assert _count(db, CampaignCandidate, id=cc.id) == 0
    assert _count(db, CandidateScoringResult, campaign_candidate_id=cc.id) == 0
    assert _count(db, CandidateOutcomeOverride, campaign_candidate_id=cc.id) == 0
    assert _count(db, ScoringShadowRanking, campaign_candidate_id=cc.id) == 0
    assert _count(db, CampaignCandidateStageHistory, campaign_candidate_id=cc.id) == 0
    # Only the application goes: the candidate, the resume and what was derived from the resume stay.
    assert _count(db, Candidate, id=scored["candidate"].id) == 1
    assert _count(db, Resume, id=scored["resume"].id) == 1
    assert _count(db, ResumeSkillEvidence, resume_id=scored["resume"].id) == 1


def test_erasing_a_scored_candidate_removes_their_scoring_and_v2_resume_data(db, scored):
    from app.repositories.candidate_repository import CandidateRepository
    from app.repositories.resume_repository import ResumeRepository
    from app.services.compliance.candidate_erasure_service import CandidateErasureService

    candidate, resume, cc = scored["candidate"], scored["resume"], scored["cc"]
    service = CandidateErasureService(
        candidate_repo=CandidateRepository(db), resume_repo=ResumeRepository(db),
        campaign_candidate_repo=CampaignCandidateRepository(db), consent_repo=MagicMock(),
        email_notification_repo=MagicMock(), celery_task_log_repo=MagicMock(), dead_letter_queue_repo=MagicMock(),
        storage_service=MagicMock(), audit_service=MagicMock(), composite_score_history_repo=MagicMock(),
        notifier=MagicMock(),
    )

    service.erase_candidate(candidate.id, actor_id="hr-1", actor_role="HR_ADMIN")

    assert _count(db, Candidate, id=candidate.id) == 0
    assert _count(db, Resume, id=resume.id) == 0
    assert _count(db, CandidateScoringResult, campaign_candidate_id=cc.id) == 0
    assert _count(db, CandidateOutcomeOverride, campaign_candidate_id=cc.id) == 0
    assert _count(db, ScoringShadowRanking, campaign_candidate_id=cc.id) == 0
    for model in (ResumeSkillEvidence, CoreSkillConfirmation, ResumeParseVersion, ResumeProvisionalSkillScan):
        assert _count(db, model, resume_id=resume.id) == 0, model.__tablename__
