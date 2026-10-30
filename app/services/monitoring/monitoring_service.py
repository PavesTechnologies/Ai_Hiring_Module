"""
The HR_ADMIN monitoring view (Phase 4): how scoring is running, from data the app already records.

- outcomes over time: decided screening outcomes (the results acted on) per campaign, by day or week;
- vote quality: of the core skills put to the vote, how many came back UNCERTAIN and how many split
  (yes and no among the votes) - from each candidate's latest v2 result in the period;
- LLM usage: calls, tokens and estimated cost per model and source, per campaign, and per resume
  (calls logged against a resume, llm_call_subject); app calls only - dry runs and golden runs apart;
- quota: today's calls per model against its daily cap (the provider's day, midnight Pacific), and
  the last 7 days;
- tasks: task statuses by type, stuck tasks, PENDING scoring results, open dead letters.

No candidate details: counts, rates and campaign names only.
"""
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from uuid import UUID

from sqlalchemy import text

from app.services.campaign.scoring_v2_service import cap_for_model, daily_llm_call_caps
from app.services.llm.call_log import QUOTA_DAY_ZONE, quota_day_start
from app.services.monitoring.llm_prices import estimate_cost, load_prices

APP_CONTEXT = "app"
STUCK_AFTER = timedelta(hours=1)


def _norm(outcome: str | None) -> str:
    return {"REVIEW": "HOLD"}.get(outcome or "", outcome or "UNKNOWN")


def _campaign_filter(alias: str, campaign_id) -> str:
    return f" and {alias}.campaign_id = :campaign_id" if campaign_id else ""


def _campaign_names(db, ids) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = db.execute(text("select id, name from hiring_campaigns where id = any(:ids)"), {"ids": list(ids)}).all()
    return {r.id: r.name for r in rows}


def outcomes_over_time(db, *, since: datetime, campaign_id: UUID | None, bucket: str) -> list[dict]:
    rows = db.execute(text(f"""
        select cc.campaign_id, date_trunc(:bucket, r.created_at) as period, r.outcome, count(*) as n
        from candidate_scoring_results r join campaign_candidates cc on cc.id = r.campaign_candidate_id
        where r.acted_on and r.created_at >= :since{_campaign_filter('cc', campaign_id)}
        group by 1, 2, 3 order by 2"""), {"bucket": bucket, "since": since, "campaign_id": campaign_id}).all()
    series: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for row in rows:
        series[row.campaign_id][row.period.date().isoformat()][_norm(row.outcome)] += row.n
    names = _campaign_names(db, series)
    return [{"campaign_id": str(cid), "campaign": names.get(cid),
             "periods": [{"period": period, **counts, "total": sum(counts.values())}
                         for period, counts in sorted(periods.items())]}
            for cid, periods in series.items()]


def vote_quality(db, *, since: datetime, campaign_id: UUID | None) -> list[dict]:
    rows = db.execute(text(f"""
        select distinct on (r.campaign_candidate_id) cc.campaign_id,
               r.breakdown -> 'confirmation' -> 'results' as results,
               r.breakdown -> 'paraphrase' -> 'results' as paraphrase
        from candidate_scoring_results r join campaign_candidates cc on cc.id = r.campaign_candidate_id
        where r.scoring_version = 'v2' and r.created_at >= :since{_campaign_filter('cc', campaign_id)}
        order by r.campaign_candidate_id, r.created_at desc"""), {"since": since, "campaign_id": campaign_id}).all()
    stats: dict = defaultdict(lambda: defaultdict(int))
    for row in rows:
        s = stats[row.campaign_id]
        s["candidates"] += 1
        for result in _as_list(row.results):
            if result.get("method") == "auto":
                s["auto_confirmed"] += 1
            if result.get("method") != "llm":
                continue
            s["voted"] += 1
            votes = result.get("votes") or []
            s["uncertain"] += result.get("status") == "UNCERTAIN"
            s["split"] += "yes" in votes and "no" in votes
        for result in _as_list(row.paraphrase):
            votes = result.get("votes") or []
            s["paraphrase_voted"] += 1
            s["paraphrase_split"] += "yes" in votes and "no" in votes
    names = _campaign_names(db, stats)
    rate = lambda part, whole: round(part / whole, 3) if whole else None  # noqa: E731
    return [{"campaign_id": str(cid), "campaign": names.get(cid), **s,
             "uncertain_rate": rate(s["uncertain"], s["voted"]), "split_rate": rate(s["split"], s["voted"]),
             "paraphrase_split_rate": rate(s["paraphrase_split"], s["paraphrase_voted"])}
            for cid, s in sorted(stats.items(), key=lambda kv: -kv[1]["candidates"])]


def _as_list(value) -> list:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [v for v in value or [] if isinstance(v, dict)]


def llm_usage(db, *, since: datetime, campaign_id: UUID | None, prices: dict) -> dict:
    by_model = db.execute(text("""
        select model, coalesce(source, 'unknown') as source, context, outcome = 'ok' as ok, count(*) as calls,
               coalesce(sum(input_tokens), 0) as tokens_in, coalesce(sum(output_tokens), 0) as tokens_out
        from llm_call_log where created_at >= :since group by 1, 2, 3, 4"""), {"since": since}).all()
    models: dict = defaultdict(lambda: defaultdict(float))
    sources: dict = defaultdict(lambda: defaultdict(float))
    contexts: dict = defaultdict(int)
    for r in by_model:
        contexts[r.context] += r.calls
        if r.context != APP_CONTEXT:
            continue
        cost = estimate_cost(prices, r.model, r.tokens_in, r.tokens_out)
        for bucket in (models[r.model], sources[r.source]):
            bucket["calls"] += r.calls
            bucket["failed"] += 0 if r.ok else r.calls
            bucket["input_tokens"] += r.tokens_in
            bucket["output_tokens"] += r.tokens_out
            bucket["cost_usd"] += cost or 0
            bucket["unpriced_calls"] += 0 if cost is not None else r.calls

    # Per resume and per campaign: calls logged against a resume or campaign (the campaign comes from the
    # resume's campaign when the call only names the resume, e.g. parsing).
    per_subject = db.execute(text("""
        select l.resume_id, coalesce(l.campaign_id, cc.campaign_id) as campaign_id, l.model,
               count(*) as calls, coalesce(sum(l.input_tokens), 0) as tokens_in,
               coalesce(sum(l.output_tokens), 0) as tokens_out
        from llm_call_log l
        left join lateral (select campaign_id from campaign_candidates c where c.resume_id = l.resume_id
                           limit 1) cc on true
        where l.created_at >= :since and l.context = :app and (l.resume_id is not null or l.campaign_id is not null)
        group by 1, 2, 3"""), {"since": since, "app": APP_CONTEXT}).all()
    resumes: dict = defaultdict(lambda: defaultdict(float))
    campaigns: dict = defaultdict(lambda: defaultdict(float))
    for r in per_subject:
        if campaign_id and r.campaign_id != campaign_id:
            continue
        cost = estimate_cost(prices, r.model, r.tokens_in, r.tokens_out) or 0
        targets = [campaigns[r.campaign_id]] + ([resumes[r.resume_id]] if r.resume_id else [])
        for bucket in targets:
            bucket["calls"] += r.calls
            bucket["tokens"] += r.tokens_in + r.tokens_out
            bucket["cost_usd"] += cost
    names = _campaign_names(db, campaigns)
    per_resume = list(resumes.values())

    def spread(key):
        values = [r[key] for r in per_resume]
        return {"mean": round(sum(values) / len(values), 4), "median": round(median(values), 4),
                "max": round(max(values), 4)} if values else None

    return {
        "by_model": [{"model": m, **{k: round(v, 6) if k == "cost_usd" else int(v) for k, v in s.items()}}
                     for m, s in sorted(models.items(), key=lambda kv: -kv[1]["calls"])],
        "by_source": [{"source": src, **{k: round(v, 6) if k == "cost_usd" else int(v) for k, v in s.items()}}
                      for src, s in sorted(sources.items(), key=lambda kv: -kv[1]["calls"])],
        "by_context": dict(contexts),
        "per_resume": {"resumes": len(per_resume), "calls": spread("calls"), "tokens": spread("tokens"),
                       "cost_usd": spread("cost_usd")},
        "by_campaign": [{"campaign_id": str(cid) if cid else None, "campaign": names.get(cid),
                         "calls": int(s["calls"]), "tokens": int(s["tokens"]), "cost_usd": round(s["cost_usd"], 6)}
                        for cid, s in sorted(campaigns.items(), key=lambda kv: -kv[1]["calls"])],
        "prices": prices,
        "note": "Costs are estimates at list price (no free tier, batch or caching discounts, or taxes).",
    }


def quota(db, *, caps: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    start = quota_day_start(now)
    week = db.execute(text("""
        select (created_at at time zone :zone)::date as day, model, count(*) as calls
        from llm_call_log where created_at >= :since group by 1, 2 order by 1"""),
        {"zone": str(QUOTA_DAY_ZONE), "since": start - timedelta(days=6)}).all()
    today = db.execute(text("select model, count(*) as calls from llm_call_log where created_at >= :start group by 1"),
                       {"start": start}).all()
    models = sorted({r.model for r in week} | {m for m in caps if m != "default"})
    used = {r.model: r.calls for r in today}
    return {
        "quota_day_started": start.isoformat(), "zone": str(QUOTA_DAY_ZONE),
        "today": [{"model": m, "calls": used.get(m, 0), "cap": cap_for_model(caps, m),
                   "share": round(used.get(m, 0) / cap_for_model(caps, m), 3) if cap_for_model(caps, m) else None}
                  for m in models],
        "last_7_days": [{"day": r.day.isoformat(), "model": r.model, "calls": r.calls} for r in week],
        "caps": caps,
    }


def task_health(db, *, since: datetime, campaign_id: UUID | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    statuses = db.execute(text("""
        select task_type, status::text as status, count(*) as n from celery_task_log
        where queued_at >= :since group by 1, 2 order by 1, 2"""), {"since": since}).all()
    stuck = db.execute(text("""
        select task_type, status::text as status, count(*) as n from celery_task_log
        where (status = 'QUEUED' and queued_at < :cutoff) or (status = 'RUNNING' and started_at < :cutoff)
        group by 1, 2"""), {"cutoff": now - STUCK_AFTER}).all()
    pending = db.execute(text(f"""
        select cc.campaign_id, x.scoring_version, count(*) as n
        from (select distinct on (campaign_candidate_id, scoring_version) campaign_candidate_id, scoring_version, outcome
              from candidate_scoring_results order by campaign_candidate_id, scoring_version, created_at desc) x
        join campaign_candidates cc on cc.id = x.campaign_candidate_id
        where x.outcome = 'PENDING'{_campaign_filter('cc', campaign_id)}
        group by 1, 2"""), {"campaign_id": campaign_id}).all()
    dead = db.execute(text("""
        select task_type, count(*) as n from dead_letter_queue where replayed_at is null group by 1""")).all()
    names = _campaign_names(db, {r.campaign_id for r in pending})
    by_type: dict = defaultdict(dict)
    for r in statuses:
        by_type[r.task_type][r.status] = r.n
    return {
        "by_task_type": [{"task_type": t, **counts} for t, counts in sorted(by_type.items())],
        "stuck": [{"task_type": r.task_type, "status": r.status, "count": r.n} for r in stuck],
        "stuck_after_minutes": int(STUCK_AFTER.total_seconds() // 60),
        "pending_results": [{"campaign_id": str(r.campaign_id), "campaign": names.get(r.campaign_id),
                             "scoring_version": r.scoring_version, "count": r.n} for r in pending],
        "open_dead_letters": [{"task_type": r.task_type, "count": r.n} for r in dead],
    }


def monitoring_summary(db, config_repository, *, days: int = 30, campaign_id: UUID | None = None,
                       now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    bucket = "day" if days <= 31 else "week"
    prices = load_prices(config_repository)
    return {
        "since": since.isoformat(), "days": days, "bucket": bucket,
        "campaign_id": str(campaign_id) if campaign_id else None,
        "outcomes_over_time": outcomes_over_time(db, since=since, campaign_id=campaign_id, bucket=bucket),
        "vote_quality": vote_quality(db, since=since, campaign_id=campaign_id),
        "llm_usage": llm_usage(db, since=since, campaign_id=campaign_id, prices=prices),
        "quota": quota(db, caps=daily_llm_call_caps(config_repository), now=now),
        "tasks": task_health(db, since=since, campaign_id=campaign_id, now=now),
    }
