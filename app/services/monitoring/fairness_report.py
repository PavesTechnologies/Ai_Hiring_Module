"""
The fairness report (HR_ADMIN): screening pass rates compared across groups the system can see
without inferring any protected attribute - experience band, career gap, highest education level,
region (state / country, never city) and internships. From each candidate's current decision (the
latest result acted on) and their resume's parse. Nothing is stored for it; erasure needs no change.

Groups smaller than FAIRNESS_MIN_GROUP_SIZE (a setting, default 10) are not shown, so no group points
to a few people. The ratio to the best group is an indicator to look into, not a legal test. A second
view counts only candidates who meet the JD's minimum experience, so a requirement isn't read as bias.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

from app.services.monitoring.regions import region_of
from app.services.resume.work_experience_duration import _entry_month_range

MIN_GROUP_SIZE_KEY = "FAIRNESS_MIN_GROUP_SIZE"
DEFAULT_MIN_GROUP_SIZE = 10
FOUR_FIFTHS = 0.8
EDUCATION_ORDER = ["UNKNOWN", "OTHER", "CERTIFICATE", "DIPLOMA", "ASSOCIATE", "BACHELOR", "POSTGRADUATE_DIPLOMA",
                   "PROFESSIONAL", "MASTER", "DOCTORATE"]
EXPERIENCE_BANDS = [(0, 1, "Under 1 year"), (1, 3, "1-3 years"), (3, 5, "3-5 years"), (5, 10, "5-10 years"),
                    (10, 200, "10+ years")]
NOTES = [
    "Shows: whether candidates in each group pass the screening at similar rates, from data the resume itself "
    "states. It never infers gender, age, religion, caste, ethnicity or disability, and can't show them.",
    "Experience bands go with age, and career gaps with caregiving or health. A gap between groups is a reason to "
    "look closer, not proof of bias; similar rates aren't proof of fairness.",
    "Many differences come from the JD's requirements (a minimum experience, a required degree). The second view "
    "counts only candidates who meet the minimum experience.",
    "Region is the state or country of the location the resume states, from a list of major cities; others show "
    "as Unknown. Groups smaller than the minimum size are hidden.",
    "The golden set's counterfactual pairs (same resume, different name, gap or region) are checked before every "
    "scoring change - that is the direct test; this report shows what happens on real decisions.",
]


def _norm(outcome: str | None) -> str:
    return {"REVIEW": "HOLD"}.get(outcome or "", outcome or "UNKNOWN")


def experience_band(years) -> str:
    if not isinstance(years, (int, float)):
        return "Unknown"
    return next(label for low, high, label in EXPERIENCE_BANDS if low <= years < high)


def longest_gap_months(parsed: dict) -> int | None:
    """The longest stretch of months with no role between the first and last role; None without dated roles."""
    covered = set()
    for entry in (parsed or {}).get("work_experience") or []:
        span = _entry_month_range(entry) if isinstance(entry, dict) else None
        if span is not None:
            covered.update(range(span[0], span[1] + 1))
    if not covered:
        return None
    longest = run = 0
    for month in range(min(covered), max(covered) + 1):
        run = run + 1 if month not in covered else 0
        longest = max(longest, run)
    return longest


def career_gap_group(parsed: dict) -> str:
    gap = longest_gap_months(parsed)
    if gap is None:
        return "Unknown"
    return "No gap over 6 months" if gap <= 6 else "Gap of 7-12 months" if gap <= 12 else "Gap over 12 months"


def education_group(parsed: dict) -> str:
    levels = [str((e or {}).get("degree_level") or "UNKNOWN").upper() for e in (parsed or {}).get("education") or []]
    ranked = [lvl for lvl in levels if lvl in EDUCATION_ORDER]
    if not ranked:
        return "Unknown"
    best = max(ranked, key=EDUCATION_ORDER.index)
    return best.replace("_", " ").capitalize()


def internship_group(parsed: dict) -> str:
    entries = (parsed or {}).get("work_experience") or []
    return "Has internships" if any(isinstance(e, dict) and e.get("is_internship") for e in entries) else "No internships"


DIMENSIONS = [
    ("experience_band", "Experience", lambda c: experience_band(c["years"])),
    ("career_gap", "Career gap", lambda c: career_gap_group(c["parsed"])),
    ("education", "Highest education", lambda c: education_group(c["parsed"])),
    ("region", "Region (state / country)", lambda c: region_of((c["parsed"] or {}).get("location"))),
    ("internships", "Internships", lambda c: internship_group(c["parsed"])),
]


def _groups(candidates: list[dict], grouper, min_size: int) -> list[dict]:
    counts: dict = defaultdict(lambda: defaultdict(int))
    for c in candidates:
        g = counts[grouper(c)]
        g["candidates"] += 1
        g["pass"] += c["outcome"] == "PASS"
        g["pass_or_hold"] += c["outcome"] in ("PASS", "HOLD")
    rows = []
    for group, g in counts.items():
        shown = g["candidates"] >= min_size
        rows.append({"group": group, "candidates": g["candidates"] if shown else None, "suppressed": not shown,
                     "pass_rate": round(g["pass"] / g["candidates"], 3) if shown else None,
                     "pass_or_hold_rate": round(g["pass_or_hold"] / g["candidates"], 3) if shown else None})
    best = max((r["pass_rate"] for r in rows if r["pass_rate"] is not None), default=None)
    for r in rows:
        r["ratio_to_best"] = round(r["pass_rate"] / best, 3) if best and r["pass_rate"] is not None else None
        r["below_four_fifths"] = r["ratio_to_best"] is not None and r["ratio_to_best"] < FOUR_FIFTHS
    return sorted(rows, key=lambda r: (r["suppressed"], r["group"]))


def min_group_size(config_repository) -> int:
    raw = config_repository.get_configs_by_keys([MIN_GROUP_SIZE_KEY]).get(MIN_GROUP_SIZE_KEY)
    try:
        return max(int(raw), 2)
    except (TypeError, ValueError):
        return DEFAULT_MIN_GROUP_SIZE


def fairness_report(db, config_repository, *, campaign_id: UUID | None = None, days: int | None = None,
                    now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days) if days else None
    rows = db.execute(text(f"""
        select distinct on (r.campaign_candidate_id) r.outcome,
               (r.breakdown -> 'experience_validation' ->> 'candidate_years')::float as years,
               (r.breakdown -> 'experience_validation' ->> 'passed') as experience_passed,
               res.parsed_json
        from candidate_scoring_results r
        join campaign_candidates cc on cc.id = r.campaign_candidate_id
        join resumes res on res.id = cc.resume_id
        where r.acted_on{" and cc.campaign_id = :campaign_id" if campaign_id else ""}
              {" and r.created_at >= :since" if since else ""}
        order by r.campaign_candidate_id, r.created_at desc"""), {"campaign_id": campaign_id, "since": since}).all()
    candidates = [{"outcome": _norm(r.outcome), "years": r.years, "parsed": r.parsed_json or {},
                   "meets_min_experience": r.experience_passed in ("true", True)}
                  for r in rows if _norm(r.outcome) in ("PASS", "HOLD", "REJECT")]
    size = min_group_size(config_repository)
    meeting = [c for c in candidates if c["meets_min_experience"]]
    return {
        "min_group_size": size, "candidates": len(candidates), "meeting_min_experience": len(meeting),
        "campaign_id": str(campaign_id) if campaign_id else None, "days": days,
        "four_fifths_ratio": FOUR_FIFTHS,
        "dimensions": [{"key": key, "label": label, "all": _groups(candidates, grouper, size),
                        "meeting_min_experience": _groups(meeting, grouper, size)}
                       for key, label, grouper in DIMENSIONS],
        "notes": NOTES,
    }
