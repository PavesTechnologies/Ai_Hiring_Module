"""
The shadow report (retire v1, phase D): the current scoring version next to the
campaign's candidate version for every candidate, plus an optional recruiter
ranking to compare both against. Read only, except the recruiter ranking (HR
admin). Trying a version on a campaign is scoring_versions.ScoringVersionService.

Per candidate: the result acted on (the current version's decision) and the
latest result of the candidate version (candidate_scoring_results), the rank
each gives, the recruiter's rank, and whether they disagree. An outcome is PASS
/ REVIEW (would_be_review) / REJECT, or PENDING (core confirmation couldn't run
yet) / NOT_RUN (daily LLM cap) / NOT_SCORED.
"""
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.services.llm.call_log import calls_in_quota_day
from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.candidates import Candidate, Resume
from app.models.pipeline import CampaignCandidate
from app.models.scoring_v2 import CandidateScoringResult, ScoringShadowRanking, ScoringVersion
from app.repositories.CampaignRepository import CampaignRepository
from app.repositories.audit_repository import AuditRepository
from app.repositories.config_repository import ConfigRepository
from app.services.audit_service import AuditService
from app.services.campaign.scoring_v2_service import cap_for_model, daily_llm_call_cap, daily_llm_call_caps
from app.services.campaign.scoring_versions import current_version

logger = logging.getLogger(__name__)

NOT_SCORED = "NOT_SCORED"
# A rank gap this large between the current and the candidate version is flagged as a disagreement.
RANK_GAP_FLAG = 3
VERSIONS = ("current", "candidate")


def outcome_label(row: CandidateScoringResult | None) -> str:
    if row is None:
        return NOT_SCORED
    breakdown = row.breakdown or {}
    if breakdown.get("would_be_review"):
        return "REVIEW"
    return row.outcome or NOT_SCORED


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
    """Adds current/candidate ranks and disagreement flags (pure - unit tested)."""
    ranks = {version: rank_by(rows, f"{version}_score") for version in VERSIONS}
    decided = ("PASS", "REVIEW", "REJECT")
    for row in rows:
        for version in VERSIONS:
            row[f"{version}_rank"] = ranks[version].get(row["campaign_candidate_id"])
        flags = []
        current, candidate = row["current_outcome"], row["candidate_outcome"]
        if current in decided and candidate in decided and current != candidate:
            flags.append(f"current {current.lower()}, candidate {candidate.lower()}")
        if (row["current_rank"] is not None and row["candidate_rank"] is not None
                and abs(row["current_rank"] - row["candidate_rank"]) >= RANK_GAP_FLAG):
            flags.append(f"rank {row['current_rank']} -> {row['candidate_rank']}")
        if row.get("recruiter_rank") is not None:
            for version in VERSIONS:
                rank = row.get(f"{version}_rank")
                if rank is not None and abs(rank - row["recruiter_rank"]) >= RANK_GAP_FLAG:
                    flags.append(f"{version} rank {rank} vs recruiter {row['recruiter_rank']}")
        row["disagreements"] = flags
    return rows


def _core_detail(prefix: str, row: CandidateScoringResult | None) -> dict:
    b = (row.breakdown or {}) if row else {}
    return {
        f"{prefix}_score": float(row.score) if row and row.score is not None else None,
        f"{prefix}_outcome": outcome_label(row),
        f"{prefix}_outcome_reason": b.get("outcome_reason"),
        f"{prefix}_core_confirmed": f"{b.get('core_confirmed_count', 0)}/{b.get('core_skill_count', 0)}" if row else None,
        f"{prefix}_failed_checks": b.get("failed_checks") or [],
        f"{prefix}_scoring_mode": getattr(row, "scoring_mode", None) if row else None,
        f"{prefix}_scored_at": row.created_at if row else None,
        f"{prefix}_config_version": row.scoring_config_version if row else None,
    }


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

    def _latest(self, cc_ids: list[UUID], candidate_version_id: UUID | None) -> tuple[dict, dict]:
        """Per candidate: the latest result acted on (current), and the candidate version's latest."""
        rows = self.db.execute(
            select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id.in_(cc_ids))
            .order_by(CandidateScoringResult.created_at.desc())
        ).scalars().all() if cc_ids else []
        current: dict = {}
        candidate: dict = {}
        for row in rows:
            if row.acted_on:
                current.setdefault(row.campaign_candidate_id, row)
            elif candidate_version_id is not None and row.scoring_version_id == candidate_version_id:
                candidate.setdefault(row.campaign_candidate_id, row)
        return current, candidate

    def report(self, campaign_id: UUID) -> dict:
        campaign = self._campaign(campaign_id)
        pairs = self.db.execute(
            select(CampaignCandidate, Candidate, Resume)
            .join(Candidate, Candidate.id == CampaignCandidate.candidate_id)
            .join(Resume, Resume.id == CampaignCandidate.resume_id)
            .where(CampaignCandidate.campaign_id == campaign_id)
        ).all()
        candidate_version = (self.db.get(ScoringVersion, campaign.candidate_scoring_version_id)
                             if getattr(campaign, "candidate_scoring_version_id", None) else None)
        current_row_version = current_version(self.db)
        current, candidate = self._latest([cc.id for cc, _, _ in pairs],
                                          candidate_version.id if candidate_version else None)
        rankings = {r.campaign_candidate_id: r for r in self.db.execute(
            select(ScoringShadowRanking).where(ScoringShadowRanking.campaign_id == campaign_id)).scalars()}

        rows, assumed_senses = [], set()
        for cc, person, resume in pairs:
            now_row, tried_row = current.get(cc.id), candidate.get(cc.id)
            shown = (tried_row or now_row)
            b = (shown.breakdown or {}) if shown else {}
            for entry in b.get("mandatory_skills") or []:
                if entry.get("jd_sense_basis") == "assumed" and entry.get("jd_sense"):
                    assumed_senses.add(f"{entry['canonical_name']} ({entry['jd_sense']})")
            ranking = rankings.get(cc.id)
            row = {
                "campaign_candidate_id": str(cc.id),
                "candidate_name": self._name(person, resume),
                "stage": cc.pipeline_stage.value,
                "parse_status": resume.parse_status.value if resume.parse_status else None,
                **_core_detail("current", now_row),
                **_core_detail("candidate", tried_row),
                # The core-skill detail of the candidate version's result (else the current one's).
                "core_confirmed_names": b.get("core_confirmed") or [],
                "core_uncertain": b.get("core_uncertain") or [],
                "core_found_not_confirmed": b.get("core_found_not_confirmed") or [],
                "core_rejected_by_llm": b.get("core_rejected_by_llm") or [],
                "core_sense_mismatch": b.get("core_sense_mismatch") or [],
                "recruiter_rank": ranking.recruiter_rank if ranking else None,
                "recruiter_ranked_by": ranking.ranked_by_name if ranking else None,
                "recruiter_note": ranking.note if ranking else None,
            }
            if row["current_score"] is None and cc.deterministic_score is not None:
                row["current_score"] = float(cc.deterministic_score)
            rows.append(row)
        rows = annotate(rows)
        rows.sort(key=lambda r: (r["current_rank"] is None, r["current_rank"] or 0, r["candidate_rank"] or 0))

        counts: dict = {version: {} for version in VERSIONS}
        for row in rows:
            for version in VERSIONS:
                outcome = row[f"{version}_outcome"]
                counts[version][outcome] = counts[version].get(outcome, 0) + 1
        label = lambda v: {"id": str(v.id), "label": v.label, "model": v.model} if v else None  # noqa: E731
        return {
            "campaign_id": str(campaign.id),
            "campaign_name": campaign.name,
            "current_version": label(current_row_version),
            "candidate_version": label(candidate_version),
            # Every LLM request in the provider's quota day (llm_call_log), as the cap counts it.
            "daily_llm_calls_used": calls_in_quota_day(self.db),
            "daily_llm_call_cap": daily_llm_call_cap(ConfigRepository(self.db)),
            "daily_llm_usage_by_model": self._usage_by_model(),
            "assumed_jd_senses": sorted(assumed_senses),
            "outcome_counts": counts,
            "disagreement_count": sum(1 for r in rows if r["disagreements"]),
            "rows": rows,
        }

    def _usage_by_model(self) -> list[dict]:
        """Each model's LLM requests in the provider's quota day, with its own cap (per-model free-tier limits)."""
        from sqlalchemy import func, select
        from app.models.llm_call_log import LLMCallLog
        from app.services.llm.call_log import quota_day_start
        caps = daily_llm_call_caps(ConfigRepository(self.db))
        used = dict(self.db.execute(
            select(LLMCallLog.model, func.count()).where(LLMCallLog.created_at >= quota_day_start())
            .group_by(LLMCallLog.model)).all())
        models = sorted(set(used) | {m for m in caps if m != "default"})
        return [{"model": m, "used": int(used.get(m, 0)), "cap": cap_for_model(caps, m)} for m in models]

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
            details={"shadow_recruiter_ranking": len(rankings), "ranked_by_name_provided": bool(ranked_by_name)},
        )
        self.db.commit()
        return self.report(campaign_id)
