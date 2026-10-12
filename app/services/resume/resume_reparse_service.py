"""
Scoring v2 Step 3: re-parse and re-score one campaign candidate's resume.

One recruiter / HR admin action:
  re-parse (parse cache bypassed) -> re-match skills -> re-embed -> re-score.

Request (API, synchronous):
  - snapshot the current parse as a resume_parse_versions row (parsed_json,
    matched skills, v2 evidence, this campaign's core skills found) - the
    previous version is always kept;
  - a REJECTED candidate is moved back to SCREENING only when a RECRUITER
    triggers it, with a reason (allowed_transitions: REJECTED -> SCREENING is
    recruiter-only and needs a reason). An HR admin can re-parse a rejected
    candidate but the stage stays REJECTED;
  - mark the resume pending and dispatch process_resume_document in
    re-parse mode.
Finish (Celery, after the new parse is committed):
  - store the diff (skills added/removed, core found before/after) on the
    version row;
  - re-embed the resume (new embedding task; the old vector is removed);
  - re-score: through the stage move (REJECTED -> SCREENING triggers the
    existing manual re-score) or enqueue_manual_rescore directly.
The new parse replaces the resume's skills for every campaign it is in;
only the requesting candidate is re-scored. The resume's candidates in OTHER
campaigns are marked "re-parsed, score out of date" (campaign_candidates.
score_out_of_date_at/_reason), shown on their scorecard, where a recruiter
can re-score them (rescore_out_of_date).

Re-score policy (request_rescore) - a re-score never moves a stage:
  SCREENING            the normal screening re-score (enqueue_manual_rescore)
  anything else        score-only (calculate_deterministic_score_task
  (past Screening, or    score_only=True): scores recomputed and the previous
  Rejected with no       ones recorded; the stage is left as it is and nothing
  recruiter move)        downstream is queued
The only stage move is REJECTED -> SCREENING, by a recruiter, with a reason,
as part of the re-parse itself.
"""
import logging
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import func, select

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.candidates import ParseStatus
from app.models.identity import UserRole
from app.models.pipeline import PipelineStage
from app.models.scoring_v2 import ResumeParseVersion, ResumeSkillEvidence
from app.models.skills import CandidateSkill, JDSkill, SkillOntology
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.campaign_candidate_repository import CampaignCandidateRepository
from app.repositories.resume_repository import ResumeRepository
from app.services.audit_service import AuditService
from app.services.resume.resume_version_diff import diff_skills

logger = logging.getLogger(__name__)

_IN_FLIGHT = {ParseStatus.PENDING, ParseStatus.PARSING}

# A re-score runs the normal screening flow only for a candidate at SCREENING;
# any other stage is score-only - a re-score never moves a stage.
FULL_RESCORE_STAGES = frozenset({PipelineStage.SCREENING})


def request_rescore(db, cc) -> str:
    """Re-score one candidate under the policy above. Returns what was done (stored in the parse diff)."""
    if cc.pipeline_stage in FULL_RESCORE_STAGES:
        from app.services.campaign.manual_candidate_rescore import enqueue_manual_rescore
        if enqueue_manual_rescore(db, cc):
            return "re-scored (screening)"
    return enqueue_score_only(db, cc)


def enqueue_score_only(db, cc) -> str:
    """Scores recomputed, stage left as it is (calculate_deterministic_score_task score_only=True)."""
    from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
    from app.services.celery_task_log_service import CeleryTaskLogService
    from app.tasks.deterministic_scoring_tasks import DETERMINISTIC_SCORE_TASK_TYPE, calculate_deterministic_score_task
    log = CeleryTaskLogService(CeleryTaskLogRepository(db)).create_log(
        task_id=str(uuid4()), task_type=DETERMINISTIC_SCORE_TASK_TYPE, campaign_candidate_id=cc.id,
    )
    calculate_deterministic_score_task.apply_async(
        kwargs={"campaign_candidate_id": str(cc.id), "score_only": True}, task_id=log.task_id,
    )
    return f"scores updated, stage unchanged ({cc.pipeline_stage.value})"


def _core_skills(db, jd_id: UUID) -> list[dict]:
    rows = db.execute(
        select(JDSkill, SkillOntology.canonical_name)
        .join(SkillOntology, SkillOntology.id == JDSkill.canonical_skill_id)
        .where(JDSkill.jd_id == jd_id, JDSkill.mandatory.is_(True))
    ).all()
    core = []
    for jd_skill, name in rows:
        importance = getattr(jd_skill.importance, "value", jd_skill.importance)
        if str(importance or "").upper() == "CORE":
            core.append({"name": name, "ids": [str(jd_skill.canonical_skill_id),
                                               *[str(i) for i in (jd_skill.alias_skill_ids or [])]]})
    return core


def snapshot_skills(db, resume_id: UUID) -> list[dict]:
    rows = db.execute(
        select(CandidateSkill, SkillOntology.canonical_name)
        .outerjoin(SkillOntology, SkillOntology.id == CandidateSkill.canonical_skill_id)
        .where(CandidateSkill.resume_id == resume_id)
    ).all()
    return [
        {"canonical_skill_id": str(cs.canonical_skill_id) if cs.canonical_skill_id else None,
         "name": name, "raw": cs.raw_extracted_text, "tier": cs.match_tier}
        for cs, name in rows
    ]


def snapshot_evidence(db, resume_id: UUID) -> list[dict]:
    rows = db.execute(
        select(ResumeSkillEvidence, SkillOntology.canonical_name)
        .join(SkillOntology, SkillOntology.id == ResumeSkillEvidence.canonical_skill_id)
        .where(ResumeSkillEvidence.resume_id == resume_id)
    ).all()
    return [
        {"canonical_skill_id": str(ev.canonical_skill_id), "name": name, "sources": list(ev.sources or []),
         "sense": ev.sense, "recognition": float(ev.recognition)}
        for ev, name in rows
    ]


def build_parse_diff(*, before_skills: list[dict], after_skills: list[dict], before_evidence: list[dict],
                     after_evidence: list[dict], core: list[dict], before_parsed: dict | None,
                     after_parsed: dict | None) -> dict:
    """What a re-parse changed - pure, so it is unit-tested directly."""
    def matched(skills):
        return {s["name"] for s in skills if s.get("canonical_skill_id") and s.get("name")}

    def unknown(skills):
        return {s["raw"] for s in skills if not s.get("canonical_skill_id") and s.get("raw")}

    def core_found(ids: set[str]) -> list[str]:
        return [c["name"] for c in core if ids & set(c["ids"])]

    before_ids = {s["canonical_skill_id"] for s in before_skills if s.get("canonical_skill_id")}
    after_ids = {s["canonical_skill_id"] for s in after_skills if s.get("canonical_skill_id")}
    before_ev = {e["canonical_skill_id"] for e in before_evidence}
    after_ev = {e["canonical_skill_id"] for e in after_evidence}
    raw = diff_skills((before_parsed or {}).get("skills") or [], (after_parsed or {}).get("skills") or [])
    core_before, core_after = core_found(before_ids), core_found(after_ids)
    v2_before, v2_after = core_found(before_ev), core_found(after_ev)
    return {
        "skills_added": sorted(matched(after_skills) - matched(before_skills)),
        "skills_removed": sorted(matched(before_skills) - matched(after_skills)),
        "unknown_added": sorted(unknown(after_skills) - unknown(before_skills)),
        "unknown_removed": sorted(unknown(before_skills) - unknown(after_skills)),
        "raw_skills_added": raw["added"],
        "raw_skills_removed": raw["removed"],
        "core_total": len(core),
        "core_matched_before": core_before,
        "core_matched_after": core_after,
        "core_v2_evidence_before": v2_before,
        "core_v2_evidence_after": v2_after,
        "core_gained": sorted(set(core_after) - set(core_before)),
        "core_lost": sorted(set(core_before) - set(core_after)),
    }


class ResumeReparseService:
    def __init__(self, db):
        self.db = db
        self.campaign_candidate_repo = CampaignCandidateRepository(db)
        self.campaign_repo = CampaignRepository(db)
        self.resume_repo = ResumeRepository(db)
        self.audit_service = AuditService(AuditRepository(db))

    # --- request (API) -------------------------------------------------------
    def request(self, *, campaign_candidate_id: UUID, actor_id: str, actor_role: str | None,
                reason: str | None) -> dict:
        cc = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if cc is None:
            raise CampaignException("Candidate not found.", 404)
        campaign = self.campaign_repo.get_by_id(cc.campaign_id)
        resume = self.resume_repo.get_by_id(cc.resume_id)
        if campaign is None or resume is None:
            raise CampaignException("Candidate's campaign or resume no longer exists.", 404)
        if resume.parse_status in _IN_FLIGHT:
            raise CampaignException("This resume is already being parsed - wait for it to finish.", 409)
        if resume.parse_status != ParseStatus.PARSED:
            raise CampaignException("Only a parsed resume can be re-parsed - use Retry for a failed parse.", 409)

        reason = (reason or "").strip() or None
        rejected = cc.pipeline_stage == PipelineStage.REJECTED
        move_to_screening = rejected and actor_role == UserRole.RECRUITER.value
        if move_to_screening and not reason:
            raise CampaignException("A reason is required to re-parse and reconsider a rejected candidate.", 422)

        version_no = (self.db.execute(
            select(func.coalesce(func.max(ResumeParseVersion.version_no), 0))
            .where(ResumeParseVersion.resume_id == resume.id)
        ).scalar_one() or 0) + 1
        version = ResumeParseVersion(
            resume_id=resume.id, version_no=version_no, parsed_json=resume.parsed_json,
            skills_snapshot=snapshot_skills(self.db, resume.id),
            evidence_snapshot=snapshot_evidence(self.db, resume.id),
            triggered_by=actor_id, reason=reason,
        )
        new_task_id = str(uuid4())
        try:
            self.db.add(version)
            self.db.flush()
            self.resume_repo.mark_parse_pending(resume)
            self.resume_repo.set_task_id(resume, new_task_id)
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role, action_type=ActionType.RESUME_REPARSE_REQUESTED,
                entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=cc.id, campaign_id=campaign.id,
                details={"resume_id": str(resume.id), "version_no": version_no, "reason": reason,
                         "move_to_screening": move_to_screening},
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        from app.tasks.resume_processing_tasks import process_resume_document
        reparse = {"version_id": str(version.id), "campaign_candidate_id": str(cc.id), "actor_id": actor_id,
                   "actor_role": actor_role, "reason": reason, "move_to_screening": move_to_screening}
        process_resume_document.apply_async(
            kwargs={"resume_id": str(resume.id), "prompt_template_id": str(campaign.prompt_template_id),
                    "reparse": reparse},
            task_id=new_task_id,
        )
        other_campaigns = sum(1 for other in self.campaign_candidate_repo.get_by_resume_id(resume.id)
                              if other.campaign_id != campaign.id)
        return {
            "campaign_candidate_id": cc.id, "resume_id": resume.id, "task_id": new_task_id,
            "version_no": version_no, "moves_to_screening": move_to_screening,
            "stays_rejected": rejected and not move_to_screening,
            "other_campaigns_sharing_resume": other_campaigns,
        }

    # --- finish (Celery, after the new parse committed) ------------------------
    def finish(self, *, resume_id: UUID, reparse: dict, task_log_service) -> dict:
        version = self.db.get(ResumeParseVersion, UUID(reparse["version_id"]))
        resume = self.resume_repo.get_by_id(resume_id)
        cc = self.campaign_candidate_repo.get_by_id(UUID(reparse["campaign_candidate_id"]))
        campaign = self.campaign_repo.get_by_id(cc.campaign_id) if cc else None

        diff = build_parse_diff(
            before_skills=version.skills_snapshot or [], after_skills=snapshot_skills(self.db, resume_id),
            before_evidence=version.evidence_snapshot or [], after_evidence=snapshot_evidence(self.db, resume_id),
            core=_core_skills(self.db, campaign.jd_id) if campaign else [],
            before_parsed=version.parsed_json, after_parsed=resume.parsed_json,
        )
        others = [other for other in self.campaign_candidate_repo.get_by_resume_id(resume_id)
                  if cc is None or other.id != cc.id]
        stamp = datetime.now(timezone.utc)
        for other in others:
            other.score_out_of_date_at = stamp
            other.score_out_of_date_reason = (
                f"The resume was re-parsed from campaign '{campaign.name if campaign else 'another campaign'}' "
                f"on {stamp:%Y-%m-%d}: its skills changed, so these scores are out of date."
            )
        diff["other_campaigns_marked_out_of_date"] = len(others)
        version.diff = {**diff, "status": "COMPLETED"}
        self.audit_service.log(
            actor_id=reparse.get("actor_id"), actor_role=reparse.get("actor_role"),
            action_type=ActionType.RESUME_REPARSED, entity_type=EntityType.CAMPAIGN_CANDIDATE,
            entity_id=cc.id if cc else resume_id, campaign_id=campaign.id if campaign else None,
            details={"resume_id": str(resume_id), "version_no": version.version_no,
                     "skills_added": len(diff["skills_added"]), "skills_removed": len(diff["skills_removed"]),
                     "core_gained": diff["core_gained"], "core_lost": diff["core_lost"]},
        )
        self.db.commit()

        self._reembed(resume, task_log_service)
        version.diff = {**version.diff, "rescore": self._rescore(cc, campaign, reparse)}
        self.db.commit()
        return version.diff

    def mark_failed(self, version_id: str, error: str) -> None:
        version = self.db.get(ResumeParseVersion, UUID(version_id))
        if version is not None:
            version.diff = {"status": "FAILED", "error": error[:300]}
            self.db.commit()

    def _reembed(self, resume, task_log_service) -> None:
        from app.tasks.embedding_tasks import EMBED_RESUME_TASK_TYPE, generate_resume_embedding_task
        try:
            self.resume_repo.delete_embedding_by_resume(resume.id)
            self.db.commit()
            log = task_log_service.create_log(task_id=str(uuid4()), task_type=EMBED_RESUME_TASK_TYPE,
                                              resume_id=resume.id)
            generate_resume_embedding_task.apply_async(kwargs={"resume_id": str(resume.id)}, task_id=log.task_id)
        except Exception:
            logger.exception("Re-parse: re-embedding could not be queued | resume_id=%s", resume.id)
            self.db.rollback()

    def _rescore(self, cc, campaign, reparse: dict) -> str:
        if cc is None or campaign is None:
            return "skipped: candidate no longer exists"
        if reparse.get("move_to_screening") and cc.pipeline_stage == PipelineStage.REJECTED:
            from app.services.campaign.bulk_stage_move_service import BulkStageMoveService
            from app.services.campaign.pipeline_transition_service import PipelineTransitionService
            from app.repositories.allowed_transition_repository import AllowedTransitionRepository
            from app.repositories.interview_schedule_repository import InterviewScheduleRepository
            try:
                BulkStageMoveService(
                    campaign_candidate_repo=self.campaign_candidate_repo,
                    pipeline_transition_service=PipelineTransitionService(
                        allowed_transition_repo=AllowedTransitionRepository(self.db),
                        campaign_candidate_repo=self.campaign_candidate_repo, audit_service=self.audit_service,
                        interview_schedule_repo=InterviewScheduleRepository(self.db),
                        campaign_repo=self.campaign_repo,
                    ),
                    audit_service=self.audit_service,
                ).move_one(
                    campaign_id=campaign.id, campaign_candidate_id=cc.id, target_stage=PipelineStage.SCREENING.value,
                    reason=f"Re-parsed resume: {reparse.get('reason')}", actor_id=reparse.get("actor_id"),
                    actor_role=reparse.get("actor_role"),
                )
                return "moved to SCREENING and re-scored"
            except Exception as exc:
                logger.exception("Re-parse: move to SCREENING failed | campaign_candidate_id=%s", cc.id)
                self.db.rollback()
                return f"move to SCREENING failed: {str(exc)[:200]}"
        return request_rescore(self.db, cc)

    # --- re-score an out-of-date candidate (API, recruiter) ---------------------
    def rescore_out_of_date(self, *, campaign_candidate_id: UUID, actor_id: str, actor_role: str | None) -> dict:
        cc = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if cc is None:
            raise CampaignException("Candidate not found.", 404)
        resume = self.resume_repo.get_by_id(cc.resume_id)
        if resume is None or resume.parse_status != ParseStatus.PARSED:
            raise CampaignException("The resume isn't parsed - it can't be re-scored right now.", 409)
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.CANDIDATE_RESCORE_REQUESTED,
            entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=cc.id, campaign_id=cc.campaign_id,
            details={"stage": cc.pipeline_stage.value, "score_out_of_date_reason": cc.score_out_of_date_reason},
        )
        self.db.commit()
        outcome = request_rescore(self.db, cc)
        return {"campaign_candidate_id": cc.id, "stage": cc.pipeline_stage.value, "rescore": outcome}

    # --- read (API) -------------------------------------------------------------
    def list_changes(self, campaign_candidate_id: UUID) -> dict:
        cc = self.campaign_candidate_repo.get_by_id(campaign_candidate_id)
        if cc is None:
            raise CampaignException("Candidate not found.", 404)
        resume = self.resume_repo.get_by_id(cc.resume_id)
        versions = self.db.execute(
            select(ResumeParseVersion).where(ResumeParseVersion.resume_id == cc.resume_id)
            .order_by(ResumeParseVersion.version_no.desc())
        ).scalars().all()
        return {
            "campaign_candidate_id": cc.id,
            "resume_id": cc.resume_id,
            "parse_status": resume.parse_status.value if resume else None,
            "versions": [
                {"version_no": v.version_no, "created_at": v.created_at, "triggered_by": v.triggered_by,
                 "reason": v.reason, "status": (v.diff or {}).get("status") or "IN_PROGRESS",
                 "diff": v.diff}
                for v in versions
            ],
        }
