"""
Provisional matching of unresolved JD unknown skills reuses the v2 scanner
rules unchanged: short terms aren't scanned, ambiguous (REQUIRE_CUE) terms
need a cue word nearby, spelling is normalised, plain words count only as
whole list items. v1 reads text rebuilt from the stored parse.
"""
from uuid import uuid4

from app.services.skills.provisional_skill_match import (
    PROVISIONAL_CREDIT, ProvisionalTerm, scan_provisional,
)
from app.services.skills.resume_skill_scanner import POLICY_EXCLUDE, POLICY_REQUIRE_CUE, ScanTermRule, term_key


def _term(text):
    return ProvisionalTerm(uuid4(), text)


def test_the_credit_is_0_8():
    assert PROVISIONAL_CREDIT == 0.8


def test_a_short_term_is_never_scanned():
    qa = _term("QA")
    found = scan_provisional("Led QA for the payments platform.\nSkills: QA, Testing", [qa], {})
    assert found == {}


def test_a_multi_word_term_matches_anywhere_with_an_excerpt():
    agentic = _term("Agentic Workflows")
    found = scan_provisional("Designed agentic workflows for claims triage.", [agentic], {})
    assert found[agentic.unknown_skill_id]["excerpt"] == "Designed agentic workflows for claims triage."
    assert found[agentic.unknown_skill_id]["origin"] == "SCAN"


def test_an_ambiguous_term_needs_a_cue_word_nearby():
    rules = {term_key("Mercury"): ScanTermRule(POLICY_REQUIRE_CUE, ("testing", "selenium"))}
    mercury = _term("Mercury")
    without_cue = "Mercury\nWorked on the Mercury programme for the board."
    with_cue = "Automated regression testing with Mercury and Selenium."
    assert scan_provisional(without_cue, [mercury], rules) == {}
    assert mercury.unknown_skill_id in scan_provisional(with_cue, [mercury], rules)


def test_an_excluded_term_never_matches():
    rules = {term_key("Delivery Excellence"): ScanTermRule(POLICY_EXCLUDE)}
    term = _term("Delivery Excellence")
    assert scan_provisional("Delivery excellence lead, 2019-2024", [term], rules) == {}


def test_british_and_american_spellings_match_each_other():
    term = _term("Programme Governance")
    assert term.unknown_skill_id in scan_provisional("Set up program governance across 12 teams.", [term], {})


def test_a_plain_word_counts_only_as_a_whole_list_item():
    term = _term("Storytelling")
    in_a_sentence = "Known for storytelling when presenting the roadmap to customers and partners."
    as_list_item = "Storytelling, Facilitation, Coaching"
    assert scan_provisional(in_a_sentence, [term], {}) == {}
    assert term.unknown_skill_id in scan_provisional(as_list_item, [term], {})


