"""
The recruiter-overrides report (HR_ADMIN): how often people move candidates against the system's
screening outcome, and the most common patterns - by reason code, by the checks and core skills
behind the outcome they overrode, by campaign and profile template. Reads candidate_outcome_overrides
only; no candidate details.

The override-rate alert lives here too (it replaced the retired HR Override Report's on 2026-11-02): a
campaign whose override rate - candidates overridden against the system's outcome / candidates with a
decided outcome - is above OVERRIDE_RATE_ALERT_THRESHOLD (%, default 20) is flagged.
"""
from collections import Counter
from datetime import datetime
from uuid import UUID

from sqlalchemy import func

from app.models.campaigns import HiringCampaign
from app.models.outcome_overrides import CandidateOutcomeOverride
from app.models.scoring_v2 import CandidateScoringResult
from app.services.campaign.outcome_overrides import KIND_OVERRIDE, KIND_REVIEW_DECISION, REASON_CODES


ALERT_THRESHOLD_KEY = "OVERRIDE_RATE_ALERT_THRESHOLD"
DEFAULT_ALERT_THRESHOLD = 20.0           # percent
RECOMMENDATION = "Review the campaign's JD calibration, core skills or thresholds."


def alert_threshold(config_repository) -> float:
    if config_repository is None:
        return DEFAULT_ALERT_THRESHOLD
    raw = config_repository.get_configs_by_keys([ALERT_THRESHOLD_KEY]).get(ALERT_THRESHOLD_KEY)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_ALERT_THRESHOLD


def _decided_counts(db, campaign_ids) -> dict:
    from app.models.pipeline import CampaignCandidate
    if not campaign_ids:
        return {}
    return dict(db.query(CampaignCandidate.campaign_id, func.count(func.distinct(CampaignCandidate.id)))
                .join(CandidateScoringResult, CandidateScoringResult.campaign_candidate_id == CampaignCandidate.id)
                .filter(CampaignCandidate.campaign_id.in_(list(campaign_ids)), CandidateScoringResult.acted_on.is_(True))
                .group_by(CampaignCandidate.campaign_id).all())


def campaign_override_rate(db, campaign_id: UUID) -> float:
    """All-time override rate of one campaign, in % (0 when nothing is decided) - the alert's rate."""
    decided = _decided_counts(db, {campaign_id}).get(campaign_id, 0)
    if not decided:
        return 0.0
    overridden = (db.query(func.count(func.distinct(CandidateOutcomeOverride.campaign_candidate_id)))
                  .filter(CandidateOutcomeOverride.campaign_id == campaign_id,
                          CandidateOutcomeOverride.kind == KIND_OVERRIDE).scalar() or 0)
    return round(overridden / decided * 100, 2)


def _counted(counter: Counter, top: int | None = None, key_name: str = "key") -> list[dict]:
    return [{key_name: key, "count": count} for key, count in counter.most_common(top)]


def override_report(db, *, campaign_id: UUID | None = None, since: datetime | None = None,
                    until: datetime | None = None, top: int = 10, config_repository=None) -> dict:
    query = db.query(CandidateOutcomeOverride)
    if campaign_id is not None:
        query = query.filter(CandidateOutcomeOverride.campaign_id == campaign_id)
    if since is not None:
        query = query.filter(CandidateOutcomeOverride.created_at >= since)
    if until is not None:
        query = query.filter(CandidateOutcomeOverride.created_at < until)
    rows = query.all()

    patterns = Counter((r.kind, r.system_outcome, r.direction, r.reason_code) for r in rows)
    by_campaign = Counter((r.campaign_id, r.kind) for r in rows)
    campaign_ids = {r.campaign_id for r in rows}
    overridden: dict = {}
    for r in rows:
        if r.kind == KIND_OVERRIDE:
            overridden.setdefault(r.campaign_id, set()).add(r.campaign_candidate_id)
    names = dict(db.query(HiringCampaign.id, HiringCampaign.name)
                 .filter(HiringCampaign.id.in_(campaign_ids)).all()) if campaign_ids else {}
    # Override rate: candidates overridden / candidates with a decided screening outcome (acted on).
    decided = _decided_counts(db, campaign_ids)
    threshold = alert_threshold(config_repository)

    return {
        "override_rate_alert_threshold": threshold,
        "total": len(rows),
        "overrides": sum(1 for r in rows if r.kind == KIND_OVERRIDE),
        "review_decisions": sum(1 for r in rows if r.kind == KIND_REVIEW_DECISION),
        "patterns": [
            {"kind": kind, "system_outcome": outcome, "direction": direction, "reason_code": code,
             "reason": REASON_CODES.get(code, code), "count": count}
            for (kind, outcome, direction, code), count in patterns.most_common(top)
        ],
        "by_reason": [{"reason_code": code, "reason": REASON_CODES.get(code, code), "count": count}
                      for code, count in Counter(r.reason_code for r in rows).most_common()],
        "by_failed_check": _counted(Counter(c for r in rows for c in (r.failed_checks or [])), top, "check"),
        "by_missing_core_skill": _counted(Counter(s for r in rows for s in (r.missing_core_skills or [])), top,
                                          "skill"),
        "by_template": _counted(Counter(r.profile_template or "none" for r in rows), None, "template"),
        "by_actor_role": _counted(Counter(r.actor_role or "unknown" for r in rows), None, "role"),
        "by_campaign": sorted(
            ({"campaign_id": str(cid), "campaign": names.get(cid),
              "overrides": by_campaign.get((cid, KIND_OVERRIDE), 0),
              "review_decisions": by_campaign.get((cid, KIND_REVIEW_DECISION), 0),
              "decided_candidates": decided.get(cid, 0),
              # the share of decided candidates overridden at least once
              "overridden_candidates": len(overridden.get(cid, ())),
              "override_rate": round(len(overridden.get(cid, ())) / decided[cid], 3) if decided.get(cid) else None,
              **_alert(len(overridden.get(cid, ())), decided.get(cid, 0), threshold)}
             for cid in campaign_ids),
            key=lambda c: -(c["overrides"] + c["review_decisions"])),
    }


def _alert(overridden: int, decided: int, threshold: float) -> dict:
    rate = overridden / decided * 100 if decided else 0.0
    flagged = rate > threshold
    return {"override_alert": flagged, "recommendation": RECOMMENDATION if flagged else None}
