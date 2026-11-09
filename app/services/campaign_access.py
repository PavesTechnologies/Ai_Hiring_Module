"""
Campaign access - the one check every campaign- or candidate-scoped route and the board websocket
uses (review items 9-12 and A-H5, product owner 2026-11-09):

- HR_ADMIN (and SUPER_ADMIN, where a route admits it) sees every campaign;
- a HIRING_MANAGER sees only the campaigns they own (hiring_campaigns.hiring_manager_id);
- a RECRUITER sees only the campaigns they are the assigned recruiter of or created
  (app/repositories/recruiter_campaigns.py - uploading to a campaign does not make it theirs).

The roles judged are the ones the route admitted (TokenUser.authorised_roles, set by
require_roles): someone who is both a hiring manager and a recruiter is judged as a recruiter on a
recruiter-only route. A denied request gets 403 "You do not have access to this campaign."; a
resource that doesn't exist gets 404, before any access is judged.

Resources reach their campaign through: campaign_candidates.campaign_id (notes, interviews and
dead letters through their campaign candidate), bulk_upload_jobs.campaign_id,
upload_decisions.campaign_id. A resume or a candidate can belong to several campaigns (a shared
resume): access to any one of them is enough. A resume no campaign links yet (an upload that
never got as far as a candidate) is its uploader's.
"""
from uuid import UUID

from sqlalchemy import false, or_, select
from sqlalchemy.orm import Session

from app.exceptions.campaign_exceptions import CampaignException
from app.middleware.rbac import ROLE_PRECEDENCE
from app.models.campaigns import HiringCampaign
from app.repositories.recruiter_campaigns import is_recruiters_campaign, recruiter_campaigns_filter

ALL_CAMPAIGNS_ROLES = frozenset({"HR_ADMIN", "SUPER_ADMIN"})
DENIED_MESSAGE = "You do not have access to this campaign."


def access_roles(user) -> tuple[str, ...]:
    """The roles the access check judges: what the route admitted, else every recognised role held."""
    admitted = tuple(getattr(user, "authorised_roles", ()) or ())
    if admitted:
        return admitted
    held = set(getattr(user, "roles", ()) or ())
    return tuple(role for role in ROLE_PRECEDENCE if role in held)


def sees_all_campaigns(user) -> bool:
    return bool(ALL_CAMPAIGNS_ROLES & set(access_roles(user)))


def can_access_campaign(user, campaign) -> bool:
    if campaign is None:
        return False
    roles = access_roles(user)
    if ALL_CAMPAIGNS_ROLES & set(roles):
        return True
    user_id = getattr(user, "user_id", None)
    if not user_id:
        return False
    if "HIRING_MANAGER" in roles and campaign.hiring_manager_id == user_id:
        return True
    return "RECRUITER" in roles and is_recruiters_campaign(campaign, user_id)


def accessible_campaigns_filter(user):
    """
    WHERE clause on hiring_campaigns for list queries: None when the user sees every campaign
    (no filter), else their campaigns under the same rule (false() when no role grants any).
    """
    roles = access_roles(user)
    if ALL_CAMPAIGNS_ROLES & set(roles):
        return None
    clauses = []
    if "HIRING_MANAGER" in roles:
        clauses.append(HiringCampaign.hiring_manager_id == user.user_id)
    if "RECRUITER" in roles:
        clauses.append(recruiter_campaigns_filter(user.user_id))
    return or_(*clauses) if clauses else false()


def accessible_campaign_ids(user):
    """SELECT of the ids of the campaigns this user may see; None when they see every campaign."""
    clause = accessible_campaigns_filter(user)
    return None if clause is None else select(HiringCampaign.id).where(clause)


def acting_user(user_id: str | None, roles) -> object:
    """A user for services that receive only an id and a role list (the interview services)."""
    from types import SimpleNamespace
    return SimpleNamespace(user_id=user_id, roles=list(roles or ()), authorised_roles=())


def _denied() -> CampaignException:
    return CampaignException(DENIED_MESSAGE, 403)


class CampaignAccess:
    """
    Loads the resource a route is about and enforces the campaign access rule on it. Every database
    read goes through the small loaders at the bottom (_get, _campaigns, ...), which tests replace.
    """

    def __init__(self, db: Session):
        self.db = db

    # -- campaigns ----------------------------------------------------------------------------

    def require_campaign(self, user, campaign_id: UUID) -> HiringCampaign:
        campaign = self._get(HiringCampaign, campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        if not can_access_campaign(user, campaign):
            raise _denied()
        return campaign

    def require_any_campaign(self, user, campaign_ids, *, owner_id: str | None = None) -> None:
        """Access to at least one of these campaigns (a shared resume or candidate)."""
        if sees_all_campaigns(user):
            return
        ids = [cid for cid in dict.fromkeys(campaign_ids) if cid is not None]
        if not ids:
            # Nothing links it to a campaign yet: only whoever uploaded it.
            if owner_id is not None and owner_id == getattr(user, "user_id", None):
                return
            raise _denied()
        if not any(can_access_campaign(user, campaign) for campaign in self._campaigns(ids)):
            raise _denied()

    def require_campaign_ids(self, user, campaign_ids) -> None:
        """Access to every one of these campaigns (a request naming several)."""
        for campaign_id in dict.fromkeys(campaign_ids):
            self.require_campaign(user, campaign_id)

    # -- things inside a campaign ---------------------------------------------------------------

    def require_campaign_candidate(self, user, campaign_candidate_id: UUID) -> HiringCampaign:
        campaign_id = self._candidate_campaign_ids([campaign_candidate_id]).get(campaign_candidate_id)
        if campaign_id is None:
            raise CampaignException("Campaign candidate not found.", 404)
        return self.require_campaign(user, campaign_id)

    def require_campaign_candidates(self, user, campaign_candidate_ids) -> None:
        """Every one of these candidates (a board's worth in one query, not one per id)."""
        ids = list(dict.fromkeys(campaign_candidate_ids or ()))
        if not ids:
            return
        owners = self._candidate_campaign_ids(ids)
        if len(owners) < len(ids):
            raise CampaignException("Campaign candidate not found.", 404)
        if sees_all_campaigns(user):
            return
        campaign_ids = set(owners.values())
        campaigns = self._campaigns(campaign_ids)
        if len(campaigns) < len(campaign_ids) or not all(can_access_campaign(user, c) for c in campaigns):
            raise _denied()

    def require_candidate_in_campaign(self, user, campaign_id: UUID, campaign_candidate_id: UUID) -> HiringCampaign:
        """A route naming both: the candidate must be in that campaign, and the campaign the user's."""
        if self._candidate_campaign_ids([campaign_candidate_id]).get(campaign_candidate_id) != campaign_id:
            raise CampaignException("Campaign candidate not found in this campaign.", 404)
        return self.require_campaign(user, campaign_id)

    def require_bulk_job(self, user, bulk_upload_job_id: UUID):
        from app.models.async_tasks import BulkUploadJob
        job = self._get(BulkUploadJob, bulk_upload_job_id)
        if job is None:
            raise CampaignException("Bulk upload job not found.", 404)
        self.require_campaign(user, job.campaign_id)
        return job

    def require_note(self, user, note_id: UUID):
        from app.models.candidate_notes import CandidateNote
        note = self._get(CandidateNote, note_id)
        if note is None:
            raise CampaignException("Note not found.", 404)
        self.require_campaign_candidate(user, note.campaign_candidate_id)
        return note

    def require_interview(self, user, interview_id: UUID):
        from app.models.interview import InterviewSchedule
        schedule = self._get(InterviewSchedule, interview_id)
        if schedule is None:
            raise CampaignException("Interview not found.", 404)
        self.require_campaign_candidate(user, schedule.campaign_candidate_id)
        return schedule

    def require_upload_decision(self, user, decision_id: UUID):
        from app.models.upload_decisions import UploadDecision
        decision = self._get(UploadDecision, decision_id)
        if decision is None:
            raise CampaignException("Upload decision not found.", 404)
        self.require_campaign(user, decision.campaign_id)
        return decision

    def require_resume(self, user, resume_id: UUID, *, every_campaign: bool = False):
        """
        every_campaign: for a destructive action (delete) on a resume shared by several campaigns,
        access to all of them - not just one.
        """
        from app.models.candidates import Resume
        resume = self._get(Resume, resume_id)
        if resume is None:
            raise CampaignException("Resume not found.", 404)
        campaign_ids = [c for c in dict.fromkeys(self._resume_campaign_ids(resume)) if c is not None]
        if every_campaign and campaign_ids:
            self.require_campaign_ids(user, campaign_ids)
        else:
            self.require_any_campaign(user, campaign_ids, owner_id=resume.uploaded_by)
        return resume

    def require_candidate(self, user, candidate_id: UUID):
        from app.models.candidates import Candidate
        candidate = self._get(Candidate, candidate_id)
        if candidate is None:
            raise CampaignException("Candidate not found.", 404)
        self.require_any_campaign(user, self._candidate_campaigns_of(candidate))
        return candidate

    def require_dead_letter(self, user, dlq_id: UUID):
        from app.models.async_tasks import DeadLetterQueue
        entry = self._get(DeadLetterQueue, dlq_id)
        if entry is None:
            raise CampaignException("Dead-letter entry not found.", 404)
        if entry.campaign_candidate_id is not None:
            self.require_campaign_candidate(user, entry.campaign_candidate_id)
        elif entry.resume_id is not None:
            self.require_resume(user, entry.resume_id)
        elif not sees_all_campaigns(user):
            raise _denied()
        return entry

    def visible_campaign_ids(self, user) -> list | None:
        """The ids of the campaigns this user may see, sorted (a stable cache key); None = every campaign."""
        query = accessible_campaign_ids(user)
        if query is None:
            return None
        return sorted(self.db.execute(query).scalars(), key=str)

    def require_task(self, user, task_id):
        """
        A background task (resume processing status): through what it is about - its campaign
        candidate, resume or bulk job; a task linked to none yet is whoever started it.
        """
        log = self._task_log(task_id)
        if log is None:
            raise CampaignException("Task not found.", 404)
        if log.campaign_candidate_id is not None:
            self.require_campaign_candidate(user, log.campaign_candidate_id)
        elif log.resume_id is not None:
            self.require_resume(user, log.resume_id)
        elif log.bulk_upload_job_id is not None:
            self.require_bulk_job(user, log.bulk_upload_job_id)
        elif not sees_all_campaigns(user) and log.created_by != getattr(user, "user_id", None):
            raise _denied()
        return log

    def accessible_resume_ids(self, user, resume_ids) -> set:
        """Which of these resumes are in a campaign the user may see (for lists)."""
        ids = set(resume_ids or ())
        if not ids or sees_all_campaigns(user):
            return ids
        return self._resumes_in_campaigns(ids, accessible_campaign_ids(user))

    # -- loaders (the only database reads) -----------------------------------------------------

    def _get(self, model, entity_id):
        return self.db.get(model, entity_id)

    def _task_log(self, task_id):
        from app.models.async_tasks import CeleryTaskLog
        return self.db.execute(select(CeleryTaskLog).where(CeleryTaskLog.task_id == str(task_id))).scalars().first()

    def _campaigns(self, campaign_ids) -> list:
        return list(self.db.execute(
            select(HiringCampaign).where(HiringCampaign.id.in_(list(campaign_ids)))).scalars().all())

    def _candidate_campaign_ids(self, campaign_candidate_ids) -> dict:
        """{campaign_candidate_id: campaign_id} for the ids that exist."""
        from app.models.pipeline import CampaignCandidate
        rows = self.db.execute(select(CampaignCandidate.id, CampaignCandidate.campaign_id)
                               .where(CampaignCandidate.id.in_(list(campaign_candidate_ids)))).all()
        return {row.id: row.campaign_id for row in rows}

    def _resume_campaign_ids(self, resume) -> list:
        from app.models.async_tasks import BulkUploadJob
        from app.models.pipeline import CampaignCandidate
        ids = list(self.db.execute(
            select(CampaignCandidate.campaign_id).where(CampaignCandidate.resume_id == resume.id)).scalars())
        if getattr(resume, "bulk_upload_job_id", None) is not None:
            ids += list(self.db.execute(
                select(BulkUploadJob.campaign_id).where(BulkUploadJob.id == resume.bulk_upload_job_id)).scalars())
        return ids

    def _candidate_campaigns_of(self, candidate) -> list:
        from app.models.pipeline import CampaignCandidate
        ids = list(self.db.execute(
            select(CampaignCandidate.campaign_id).where(CampaignCandidate.candidate_id == candidate.id)).scalars())
        return ids + [getattr(candidate, "source_campaign_id", None)]

    def _resumes_in_campaigns(self, resume_ids, campaign_ids_select) -> set:
        from app.models.async_tasks import BulkUploadJob
        from app.models.candidates import Resume
        from app.models.pipeline import CampaignCandidate
        via_candidates = select(CampaignCandidate.resume_id).where(
            CampaignCandidate.resume_id.in_(resume_ids), CampaignCandidate.campaign_id.in_(campaign_ids_select))
        via_jobs = (select(Resume.id).join(BulkUploadJob, Resume.bulk_upload_job_id == BulkUploadJob.id)
                    .where(Resume.id.in_(resume_ids), BulkUploadJob.campaign_id.in_(campaign_ids_select)))
        return set(self.db.execute(via_candidates).scalars()) | set(self.db.execute(via_jobs).scalars())
