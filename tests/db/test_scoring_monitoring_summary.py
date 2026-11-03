"""The scoring monitoring summary against real Postgres (rolled back): outcomes, vote quality, LLM usage, tasks."""
from datetime import datetime, timezone
from unittest.mock import MagicMock

from sqlalchemy import select

from app.models.llm_call_log import LLMCallLog
from app.models.scoring_v2 import CandidateScoringResult
from app.services.monitoring.llm_prices import DEFAULT_LLM_MODEL_PRICES
from app.services.monitoring.monitoring_service import monitoring_summary
from tests.db.test_campaign_candidate_delete_scored import db, scored  # noqa: F401 - fixtures


def _config():
    import json
    repo = MagicMock()
    repo.get_configs_by_keys.side_effect = lambda keys: {
        "LLM_MODEL_PRICES": json.dumps(DEFAULT_LLM_MODEL_PRICES),
        "SCORING_V2_DAILY_LLM_CALL_CAP": '{"default": 400, "gemini-3.1-flash-lite": 400}'}
    return repo


def test_the_summary_counts_outcomes_votes_and_usage_for_one_campaign(db, scored):  # noqa: F811
    cc, campaign, resume = scored["cc"], scored["campaign"], scored["resume"]
    v1 = db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc.id,
                                                        CandidateScoringResult.acted_on.is_(True))).scalar_one()
    v1.outcome = "REJECT"
    v2 = db.execute(select(CandidateScoringResult).where(CandidateScoringResult.campaign_candidate_id == cc.id,
                                                        CandidateScoringResult.scoring_version == "v2")).scalar_one()
    v2.breakdown = {"confirmation": {"results": [
        {"name": "A", "method": "llm", "status": "UNCERTAIN", "votes": ["yes", "no", "no"]},
        {"name": "B", "method": "llm", "status": "CONFIRMED", "votes": ["yes", "yes", "yes"]},
        {"name": "C", "method": "auto", "status": "CONFIRMED", "votes": []}]}}
    db.add_all([
        LLMCallLog(provider="GOOGLE", model="gemini-3.1-flash-lite", outcome="ok", context="app",
                   source="skill_confirmation", input_tokens=1_000_000, output_tokens=0,
                   resume_id=resume.id, campaign_id=campaign.id),
        LLMCallLog(provider="GOOGLE", model="gemini-3.1-flash-lite", outcome="ok", context="app",
                   source="resume_parse", input_tokens=0, output_tokens=1_000_000, resume_id=resume.id),
        LLMCallLog(provider="GOOGLE", model="gemini-3.1-flash-lite", outcome="ok", context="golden_run",
                   source="skill_confirmation", input_tokens=5, output_tokens=5),
    ])
    db.flush()

    summary = monitoring_summary(db, _config(), days=7, campaign_id=campaign.id, now=datetime.now(timezone.utc))

    [series] = summary["outcomes_over_time"]
    assert series["campaign"] == campaign.name and sum(p.get("REJECT", 0) for p in series["periods"]) == 1
    [quality] = summary["vote_quality"]
    assert (quality["candidates"], quality["voted"], quality["auto_confirmed"]) == (1, 2, 1)
    assert quality["uncertain_rate"] == 0.5 and quality["split_rate"] == 0.5
    usage = summary["llm_usage"]
    # The parse call names only the resume; its campaign comes from the resume's campaign candidate.
    [mine] = [c for c in usage["by_campaign"] if c["campaign_id"] == str(campaign.id)]
    assert mine["calls"] == 2 and mine["cost_usd"] == 0.25 + 1.5
    assert usage["per_resume"]["resumes"] == 1 and usage["per_resume"]["calls"]["mean"] == 2
    assert usage["by_context"]["golden_run"] >= 1                     # counted apart from app usage
    assert any(q["model"] == "gemini-3.1-flash-lite" and q["cap"] == 400 for q in summary["quota"]["today"])
    assert isinstance(summary["tasks"]["by_task_type"], list)


def test_rejected_reparses_are_counted_per_campaign_and_recruiter_not_as_overrides(db, scored):  # noqa: F811
    from sqlalchemy import text
    from app.enums.constants import ActionType, EntityType
    from app.models.compliance import AuditLog
    cc, campaign = scored["cc"], scored["campaign"]
    user_id, user_name = db.execute(text("select id, full_name from users order by created_at limit 1")).one()

    def reparse(detail, actor=user_id):
        return AuditLog(actor_id=actor, actor_role="RECRUITER", action_type=ActionType.RESUME_REPARSE_REQUESTED,
                        entity_type=EntityType.CAMPAIGN_CANDIDATE, entity_id=cc.id, campaign_id=campaign.id,
                        detail={"version_no": 1, "reason_provided": True, **detail})
    db.add_all([
        reparse({"move_to_screening": True, "was_rejected": True}),
        reparse({"move_to_screening": False, "was_rejected": True}),      # HR_ADMIN: stays rejected
        reparse({"move_to_screening": True}),                             # before was_rejected was recorded
        reparse({"move_to_screening": False, "was_rejected": False}),     # not a rejected candidate
        reparse({"move_to_screening": False, "was_rejected": True}, actor=None),
    ])
    db.flush()

    section = monitoring_summary(db, _config(), days=7, campaign_id=campaign.id,
                                 now=datetime.now(timezone.utc))["rejected_reparses"]

    assert section["total"] == 4
    assert section["by_campaign"] == [{"campaign_id": str(campaign.id), "campaign": campaign.name,
                                       "count": 4, "moved_to_screening": 2}]
    assert {r["actor_id"]: (r["recruiter"], r["count"]) for r in section["by_recruiter"]} == {
        str(user_id): (user_name, 3), None: (None, 1)}
