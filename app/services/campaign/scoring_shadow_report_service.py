"""
Scoring v2 Step 4: the shadow report - v1 next to v2 for every candidate of a
campaign, plus an optional recruiter ranking to compare both against. Read
only, except the recruiter ranking and the campaign's shadow switch (HR admin).

Per candidate: the latest v1 and v2 results (candidate_scoring_results), the
rank each version gives, the recruiter's rank, and whether v1 and v2 disagree.
v2's outcome is PASS / REVIEW (would_be_review) / REJECT, or PENDING (core
confirmation couldn't run yet) / NOT_RUN (daily LLM cap) / NOT_SCORED.
"""
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.candidates import Candidate, ParseStatus, Resume
from app.models.pipeline import CampaignCandidate
from app.models.scoring_v2 import CandidateScoringResult, ScoringShadowRanking
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.config_repository import ConfigRepository
from app.repositories.scoring_result_repository import ScoringResultRepository
from app.services.audit_service import AuditService
from app.services.campaign.scoring_v2_service import daily_llm_call_cap

logger = logging.getLogger(__name__)

NOT_SCORED = "NOT_SCORED"
# A rank gap this large between v1 and v2 is flagged as a disagreement.
RANK_GAP_FLAG = 3


def v2_label(row: CandidateScoringResult | None) -> str:
    if row is None:
        return NOT_SCORED
    breakdown = row.breakdown or {}
    if breakdown.get("would_be_review"):
        return "REVIEW"
    return row.outcome or NOT_SCORED


def v1_label(row: CandidateScoringResult | None, cc) -> str:
    if row is not None:
        return row.outcome or NOT_SCORED
    if getattr(cc, "deterministic_passed", None) is None:
        return NOT_SCORED
    return "PASS" if cc.deterministic_passed else "REJECT"


def rank_by(rows: list[dict], score_key: str) -> dict:
    """1 = best; only rows with a score are ranked; ties share a rank."""
    scored = sorted((r for r in rows if r.get(score_key) is not None), key=lambda r: -r[score_key])
    ranks, previous, rank = {}, None, 0
    for index, row in enumerate(scored, start=1):
        if row[score_key] != previous:
            rank, previous = index, row[score_key]
        ranks[row["campaign_candidate_id"]] = rank
    return ranks


def annotate(rows: list[dict]) -> list[dict]:
    """Adds v1/v2 ranks and disagreement flags (pure - unit tested)."""
    v1_ranks, v2_ranks = rank_by(rows, "v1_score"), rank_by(rows, "v2_score")
    for row in rows:
        row["v1_rank"] = v1_ranks.get(row["campaign_candidate_id"])
        row["v2_rank"] = v2_ranks.get(row["campaign_candidate_id"])
        flags = []
        v1, v2 = row["v1_outcome"], row["v2_outcome"]
        if v2 in ("PASS", "REVIEW", "REJECT") and v1 in ("PASS", "REJECT"):
            if v1 == "REJECT" and v2 in ("PASS", "REVIEW"):
                flags.append(f"v1 rejects, v2 {v2.lower()}")
            elif v1 == "PASS" and v2 == "REJECT":
                flags.append("v1 passes, v2 rejects")
        if row["v1_rank"] is not None and row["v2_rank"] is not None and abs(row["v1_rank"] - row["v2_rank"]) >= RANK_GAP_FLAG:
            flags.append(f"rank {row['v1_rank']} -> {row['v2_rank']}")
        if row.get("recruiter_rank") is not None:
            for version in ("v1", "v2"):
                rank = row.get(f"{version}_rank")
                if rank is not None and abs(rank - row["recruiter_rank"]) >= RANK_GAP_FLAG:
                    flags.append(f"{version} rank {rank} vs recruiter {row['recruiter_rank']}")
        row["disagreements"] = flags
    return rows


class ScoringShadowReportService:
    def __init__(self, db, encryption_service=None):
        self.db = db
        self.encryption_service = encryption_service
        self.campaign_repo = CampaignRepository(db)
        self.audit_service = AuditService(AuditRepository(db))

    def _campaign(self, campaign_id: UUID):
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        return campaign

    def _name(self, candidate, resume) -> str:
        if candidate is not None and self.encryption_service is not None and candidate.full_name_encrypted:
            try:
                return self.encryption_service.decrypt(candidate.full_name_encrypted, candidate.encryption_key_id)
            except Exception:
                logger.warning("Could not decrypt candidate name | candidate_id=%s", candidate.id)
        return (getattr(resume, "original_filename", None) or "Candidate")[:80]

    def _latest(self, cc_ids: list[UUID]) -> dict:
        rows = self.db.execute(
            select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id.in_(cc_ids))
            .order_by(CandidateScoringResult.created_at.desc())
        ).scalars().all() if cc_ids else []
        latest: dict = {}
        for row in rows:
            latest.setdefault((row.campaign_candidate_id, row.scoring_version), row)
        return latest

    def report(self, campaign_id: UUID) -> dict:
        campaign = self._campaign(campaign_id)
        pairs = self.db.execute(
            select(CampaignCandidate, Candidate, Resume)
            .join(Candidate, Candidate.id == CampaignCandidate.candidate_id)
            .join(Resume, Resume.id == CampaignCandidate.resume_id)
            .where(CampaignCandidate.campaign_id == campaign_id)
        ).all()
        latest = self._latest([cc.id for cc, _, _ in pairs])
        rankings = {r.campaign_candidate_id: r for r in self.db.execute(
            select(ScoringShadowRanking).where(ScoringShadowRanking.campaign_id == campaign_id)).scalars()}

        rows, assumed_senses = [], set()
        for cc, candidate, resume in pairs:
            v1 = latest.get((cc.id, "v1"))
            v2 = latest.get((cc.id, "v2"))
            b2 = (v2.breakdown or {}) if v2 else {}
            for entry in b2.get("mandatory_skills") or []:
                if entry.get("jd_sense_basis") == "assumed" and entry.get("jd_sense"):
                    assumed_senses.add(f"{entry['canonical_name']} ({entry['jd_sense']})")
            ranking = rankings.get(cc.id)
            v1_score = float(v1.score) if v1 and v1.score is not None else (
                float(cc.deterministic_score) if cc.deterministic_score is not None else None)
            rows.append({
                "campaign_candidate_id": str(cc.id),
                "candidate_name": self._name(candidate, resume),
                "stage": cc.pipeline_stage.value,
                "parse_status": resume.parse_status.value if resume.parse_status else None,
                "v1_score": v1_score,
                "v1_outcome": v1_label(v1, cc),
                "v2_score": float(v2.score) if v2 and v2.score is not None else None,
                "v2_outcome": v2_label(v2),
                "v2_outcome_reason": b2.get("outcome_reason"),
                "v2_core_confirmed": f"{b2.get('core_confirmed_count', 0)}/{b2.get('core_skill_count', 0)}" if v2 else None,
                "v2_core_confirmed_names": b2.get("core_confirmed") or [],
                "v2_core_uncertain": b2.get("core_uncertain") or [],
                "v2_core_found_not_confirmed": b2.get("core_found_not_confirmed") or [],
                "v2_core_rejected_by_llm": b2.get("core_rejected_by_llm") or [],
                "v2_core_sense_mismatch": b2.get("core_sense_mismatch") or [],
                "v2_failed_checks": b2.get("failed_checks") or [],
                "v2_scored_at": v2.created_at if v2 else None,
                "v2_config_version": v2.scoring_config_version if v2 else None,
                "recruiter_rank": ranking.recruiter_rank if ranking else None,
                "recruiter_ranked_by": ranking.ranked_by_name if ranking else None,
                "recruiter_note": ranking.note if ranking else None,
            })
        rows = annotate(rows)
        rows.sort(key=lambda r: (r["v2_rank"] is None, r["v2_rank"] or 0, r["v1_rank"] or 0))

        counts: dict = {}
        for row in rows:
            counts[row["v2_outcome"]] = counts.get(row["v2_outcome"], 0) + 1
        return {
            "campaign_id": str(campaign.id),
            "campaign_name": campaign.name,
            "scoring_v2_shadow": bool(getattr(campaign, "scoring_v2_shadow", False)),
            "scoring_v2_enabled": bool(getattr(campaign, "scoring_v2_enabled", False)),
            "daily_llm_calls_used": ScoringResultRepository(self.db).v2_llm_calls_today(),
            "daily_llm_call_cap": daily_llm_call_cap(ConfigRepository(self.db)),
            "assumed_jd_senses": sorted(assumed_senses),
            "v2_outcome_counts": counts,
            "disagreement_count": sum(1 for r in rows if r["disagreements"]),
            "rows": rows,
        }

    def save_recruiter_ranking(self, campaign_id: UUID, *, rankings: list[dict], ranked_by_name: str | None,
                               actor_id: str, actor_role: str | None) -> dict:
        self._campaign(campaign_id)
        valid = {cc_id for (cc_id,) in self.db.execute(
            select(CampaignCandidate.id).where(CampaignCandidate.campaign_id == campaign_id)).all()}
        existing = {r.campaign_candidate_id: r for r in self.db.execute(
            select(ScoringShadowRanking).where(ScoringShadowRanking.campaign_id == campaign_id)).scalars()}
        now = datetime.now(timezone.utc)
        for item in rankings:
            cc_id = UUID(str(item["campaign_candidate_id"]))
            if cc_id not in valid:
                raise CampaignException("A ranked candidate isn't in this campaign.", 422)
            row = existing.get(cc_id) or ScoringShadowRanking(campaign_id=campaign_id, campaign_candidate_id=cc_id)
            row.recruiter_rank = item.get("rank")
            row.note = item.get("note")
            row.ranked_by_name = ranked_by_name
            row.entered_by = actor_id
            row.updated_at = now
            self.db.add(row)
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.CAMPAIGN_UPDATED,
            entity_type=EntityType.CAMPAIGN, entity_id=campaign_id, campaign_id=campaign_id,
            details={"scoring_v2_shadow_recruiter_ranking": len(rankings), "ranked_by": ranked_by_name},
        )
        self.db.commit()
        return self.report(campaign_id)

    def set_shadow(self, campaign_id: UUID, *, enabled: bool, backfill: bool, actor_id: str,
                   actor_role: str | None) -> dict:
        """Opt a campaign in/out of v2 shadow; backfill queues v2 for its already-scored candidates."""
        campaign = self._campaign(campaign_id)
        campaign.scoring_v2_shadow = enabled
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.CAMPAIGN_UPDATED,
            entity_type=EntityType.CAMPAIGN, entity_id=campaign_id, campaign_id=campaign_id,
            details={"scoring_v2_shadow": enabled, "backfill": backfill},
        )
        self.db.commit()
        queued = 0
        if enabled and backfill:
            from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
            from app.services.celery_task_log_service import CeleryTaskLogService
            from app.tasks.scoring_v2_tasks import enqueue_scoring_v2
            log_service = CeleryTaskLogService(CeleryTaskLogRepository(self.db))
            candidates = self.db.execute(
                select(CampaignCandidate).join(Resume, Resume.id == CampaignCandidate.resume_id)
                .where(CampaignCandidate.campaign_id == campaign_id, Resume.parse_status == ParseStatus.PARSED,
                       CampaignCandidate.deterministic_score.isnot(None))
            ).scalars().all()
            for cc in candidates:
                queued += int(bool(enqueue_scoring_v2(cc, log_service)))
            self.db.commit()
        return {"scoring_v2_shadow": enabled, "queued": queued}
