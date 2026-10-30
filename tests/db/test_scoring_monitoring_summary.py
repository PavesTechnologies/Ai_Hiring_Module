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
