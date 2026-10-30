"""The golden runner's comparisons: expectations, counterfactual pairs, the accepted run, and their causes."""
from types import SimpleNamespace

from scripts.golden.run import GoldenVotes, cause, check_expectations, compare_accepted, compare_pair, summarise


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
