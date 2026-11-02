"""The golden runner's comparisons: expectations, counterfactual pairs, the accepted run, and their causes."""
import datetime
from decimal import Decimal
from types import SimpleNamespace

from scripts.golden.run import (
    GoldenVotes, cause, check_expectations, compare_accepted, compare_pair, has_ongoing_role, numeric_inputs,
    only_calendar, summarise,
)


def _skill(status="CONFIRMED", method="llm", votes=("yes", "yes", "yes"), evidence=("[EXPERIENCE] Built X",),
           paraphrase=None):
    return {"status": status, "method": method, "reason": None, "votes": list(votes), "quote": "",
            "paraphrase": paraphrase, "evidence": list(evidence)}


def _result(outcome="PASS", core="2/2", **skills):
    return {"outcome": outcome, "core": core, "skills": skills or {"Python": _skill(), "SQL": _skill()},
            "llm_calls": 0, "problem": None, "failed_checks": []}


def test_expectations_check_the_outcome_and_an_exact_core_count():
    case = {"expected": {"outcomes": ["HOLD", "REJECT"], "core": None}}
    assert check_expectations(case, _result("REJECT")) == []
    assert check_expectations(case, _result("PASS")) == ["outcome PASS, expected HOLD/REJECT"]
    exact = {"expected": {"outcomes": ["PASS"], "core": "2/2"}}
    assert check_expectations(exact, _result(core="1/2")) == ["core 1/2, expected 2/2"]


def test_a_pending_result_is_a_failure_with_its_reason():
    pending = {**_result("PENDING"), "problem": "golden vote cache miss"}
    assert any("golden vote cache miss" in p for p in check_expectations({"expected": {"outcomes": ["PASS"]}}, pending))


def test_a_counterfactual_pair_reports_outcome_and_skill_differences_with_causes():
    base = _result("PASS", "2/2")
    variant = _result("HOLD", "1/2", Python=_skill(), SQL=_skill("UNCERTAIN", votes=("yes", "no", "no"),
                                                                  evidence=("[SKILLS] SQL",)))
    found = compare_pair({"id": "CF-1"}, {"id": "F1"}, base, variant)
    assert found[0] == "outcome HOLD vs PASS (F1)"
    assert "SQL: UNCERTAIN vs CONFIRMED (F1)" in found[1] and "evidence" in found[1] and "votes" in found[1]
    assert compare_pair({"id": "CF-1"}, {"id": "F1"}, base, _result("PASS", "2/2")) == []


def test_the_cause_names_what_changed_first_evidence_then_method_votes_paraphrase():
    old = _skill(method="auto", votes=())
    new = _skill(method="llm", votes=("yes", "yes", "yes"))
    assert cause(old, new) == "method auto -> llm; votes [] -> ['yes', 'yes', 'yes']"
    para_old = _skill("UNCERTAIN", paraphrase={"status": "REJECTED", "votes": ["no"] * 3, "lines": ["a"]})
    para_new = _skill("CONFIRMED", paraphrase={"status": "CONFIRMED", "votes": ["yes"] * 3, "lines": ["a"]})
    assert cause(para_old, para_new).startswith("paraphrase")
    assert cause(None, _skill()) == "skill added or removed from the JD's core skills"


def test_changes_since_the_accepted_run_include_a_method_change_with_the_same_status():
    accepted = {"cases": {"H7": _result("REJECT", "1/2", Python=_skill(method="auto", votes=()), SQL=_skill())}}
    now = _result("REJECT", "1/2", Python=_skill(method="llm"), SQL=_skill())
    changes = compare_accepted("H7", now, accepted)
    assert len(changes) == 1 and changes[0].startswith("Python: CONFIRMED (auto) -> CONFIRMED (llm)")
    assert compare_accepted("NEW", now, accepted) == ["new case (not in the accepted run)"]


def _numbers(years, ongoing=False, **inputs):
    return {**_result("PASS"), "experience_years": years, "experience_ongoing": ongoing, "score": None,
            "numeric_inputs": inputs}


def test_changes_since_the_accepted_run_include_experience_years_and_numeric_inputs():
    accepted = {"run_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "cases": {"S7": _numbers(20.1, **{"experience.min_years": 12.0, "domain.score": 1.0})}}
    now = _numbers(19.0, **{"experience.min_years": 12.0, "domain.score": 0.5, "education.score": 1.0})
    assert compare_accepted("S7", now, accepted) == [
        "experience years 20.1 -> 19.0", "domain.score 1.0 -> 0.5", "education.score None -> 1.0"]
    assert compare_accepted("S7", accepted["cases"]["S7"], accepted) == []


def test_an_ongoing_roles_monthly_growth_is_listed_as_calendar_drift():
    two_months_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=62)
    accepted = {"run_at": two_months_ago.isoformat(),
                "cases": {"S2": _numbers(11.1, True, **{"experience.score": 0.9}),
                          "D1": _numbers(6.3, False)}}
    drift = compare_accepted("S2", _numbers(11.3, True, **{"experience.score": 0.95}), accepted)
    assert len(drift) == 2 and only_calendar(drift)
    assert "calendar: an ongoing role, 2 month(s)" in drift[0]
    # More than the calendar explains, or no ongoing role: a real change.
    assert not only_calendar(compare_accepted("S2", _numbers(12.5, True, **{"experience.score": 0.9}), accepted))
    assert not only_calendar(compare_accepted("D1", _numbers(6.5, False), accepted))


def test_an_accepted_run_without_numeric_inputs_still_compares_experience_years():
    old = {k: v for k, v in _numbers(5.0).items() if k != "numeric_inputs"}
    assert compare_accepted("X", _numbers(5.0, **{"domain.score": 1.0}), {"cases": {"X": old}}) == []
    assert compare_accepted("X", _numbers(5.5), {"cases": {"X": old}}) == ["experience years 5.0 -> 5.5"]


def test_the_summary_records_every_numeric_check_input_once():
    inputs = numeric_inputs({"candidate_years": 9, "min_years": 8.0, "near_miss_tolerance_years": None, "passed": True},
                            {"score": Decimal("1.5"), "required": "B.E.", "required_field": None},
                            {"score": 0.5, "applicable": True})
    assert inputs == {"domain.score": 0.5, "education.score": 1.5, "experience.min_years": 8.0,
                      "experience.near_miss_tolerance_years": None}
    assert has_ongoing_role({"work_experience": [{"start_date": "Jan 2020", "end_date": "Present"}]})
    assert not has_ongoing_role({"work_experience": [{"start_date": "Jan 2020", "end_date": "Present",
                                                      "title": "Career break"}]})
    assert not has_ongoing_role({"work_experience": [{"start_date": "Jan 2020", "end_date": "Dec 2021"}]})


def test_a_paraphrase_confirmation_counts_as_confirmed_in_the_summary():
    sid = "11111111-1111-1111-1111-111111111111"
    breakdown = {"outcome": "REVIEW", "core_confirmed_count": 1, "core_skill_count": 1, "failed_checks": [],
                 "confirmation": {"llm_calls": 3, "results": [
                     {"canonical_skill_id": sid, "name": "MLOps", "status": "UNCERTAIN", "method": "llm",
                      "votes": ["yes", "yes", "yes"], "reason": "listed_only"}]},
                 "paraphrase": {"results": [{"canonical_skill_id": sid, "status": "CONFIRMED",
                                             "votes": ["yes"] * 3}],
                                "proposals": [{"canonical_skill_id": sid, "line": "Deployed models"}]}}
    record = SimpleNamespace(canonical_skill_id=sid, evidence=[{"section": "SKILLS", "text": "MLOps"}])
    result = summarise(breakdown, [record], {"candidate_years": 9})
    assert result["outcome"] == "HOLD" and result["core"] == "1/1"
    assert result["skills"]["MLOps"]["status"] == "CONFIRMED" and result["skills"]["MLOps"]["method"] == "paraphrase"
    assert result["skills"]["MLOps"]["evidence"] == ["[SKILLS] MLOps"]


def test_votes_come_from_the_golden_cache_then_dev_and_new_answers_are_kept_for_accept():
    dev_row = SimpleNamespace(results=[{"x": 1}], provider="GOOGLE", model="m", used_fallback=False)
    repo = SimpleNamespace(get_confirmation=lambda key: dev_row if key == "in-dev" else None, other="delegated")
    votes = GoldenVotes(repo, {"golden": {"results": [], "provider": "GOOGLE", "model": "m", "used_fallback": False}},
                        dev_cache=True)
    assert votes.get_confirmation("golden").model == "m" and votes.hits == 1
    assert votes.get_confirmation("in-dev") is dev_row and votes.dev_hits == 1 and "in-dev" in votes.new
    assert votes.get_confirmation("nowhere") is None
    assert votes.other == "delegated"
    outcome = SimpleNamespace(cache_key="asked", llm_calls=3, call=SimpleNamespace(provider="GOOGLE", model="m",
                              used_fallback=False), results=[{"evidence_hash": "e", "status": "CONFIRMED"},
                                                             {"status": "NOT_FOUND"}])
    votes.record(outcome)
    assert votes.new["asked"]["results"] == [{"evidence_hash": "e", "status": "CONFIRMED"}]
    no_dev = GoldenVotes(repo, {}, dev_cache=False)
    assert no_dev.get_confirmation("in-dev") is None


def test_new_votes_are_kept_per_case_and_found_before_the_shared_entry():
    shared = {"results": [{"x": "shared"}], "provider": "GOOGLE", "model": "m", "used_fallback": False}
    votes = GoldenVotes(SimpleNamespace(get_confirmation=lambda key: None), {"K": shared}, dev_cache=False)
    votes.case_id = "S2"
    assert votes.get_confirmation("K").results == [{"x": "shared"}]          # older plain entries still serve
    outcome = SimpleNamespace(cache_key="K", llm_calls=3, results=[{"x": "s2", "evidence_hash": "h"}],
                              call=SimpleNamespace(provider="GOOGLE", model="m", used_fallback=False))
    votes.record(outcome)
    assert list(votes.new) == ["S2|K"]
    assert votes.get_confirmation("K").results == [{"x": "s2", "evidence_hash": "h"}]   # the case's own first
    votes.case_id = "CF-NAME-S2-0"
    assert votes.get_confirmation("K").results == [{"x": "shared"}]          # the other case keeps the shared one
