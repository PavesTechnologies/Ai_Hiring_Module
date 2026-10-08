"""Scoring v2 detection: British/American spelling, wrapped-line excerpts, DB pattern rules."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.skills.resume_sections import section_of_line
from app.services.skills.resume_skill_scanner import PatternRule, ScanIndex, pattern_hits, scan_text, term_key
from app.services.skills.skill_evidence_service import (
    SOURCE_DERIVED, LLMSkillMatch, build_evidence, excerpt_at, resolve_blocklist,
)
from app.services.skills.skill_sense_resolver import SkillSenseResolver
from app.services.skills.spelling import normalise_word

PM, ORG, OPT, DM, CM, AIG, BEH, CAT = (uuid4() for _ in range(8))
CATALOG = [
    SimpleNamespace(id=PM, canonical_name="Program Management", aliases=[]),
    SimpleNamespace(id=ORG, canonical_name="Organizational Design", aliases=[]),
    SimpleNamespace(id=OPT, canonical_name="Cost Optimization", aliases=[]),
    SimpleNamespace(id=DM, canonical_name="Data Modeling", aliases=[]),
    SimpleNamespace(id=CM, canonical_name="Change Management", aliases=[]),
    SimpleNamespace(id=AIG, canonical_name="AI Governance", aliases=[]),
    SimpleNamespace(id=BEH, canonical_name="Behavioral Analytics", aliases=[]),
    SimpleNamespace(id=CAT, canonical_name="Service Catalog Management", aliases=[]),
]
AI_GOV_RULE = PatternRule(AIG, "AI near governance (Experience)", ("AI", "responsible AI"), ("governance",), 3,
                          frozenset({"EXPERIENCE"}), True)


def _found(text):
    return {hit.skill_id for hit in scan_text(text, ScanIndex.build(CATALOG, {}))}


# --- spelling -------------------------------------------------------------

@pytest.mark.parametrize("british, american", [
    ("programme", "program"), ("organisation", "organization"), ("organisational", "organizational"),
    ("optimisation", "optimization"), ("optimise", "optimize"), ("prioritising", "prioritizing"),
    ("modelling", "modeling"), ("labelled", "labeled"), ("behaviour", "behavior"), ("catalogue", "catalog"),
    ("centre", "center"), ("analyse", "analyze"), ("analysing", "analyzing"), ("licence", "license"),
])
def test_british_spellings_normalise(british, american):
    assert normalise_word(british) == american


@pytest.mark.parametrize("word", [
    "expertise", "enterprise", "advise", "exercise", "franchise", "promise", "supervise", "rise", "noise",
    "selling", "telling", "spelling", "filling", "billing", "controlling", "installing",
    "hour", "four", "your", "tour", "contour", "precise", "concise", "wise", "analysis", "program",
])
def test_ordinary_words_are_left_alone(word):
    assert normalise_word(word) == word


@pytest.mark.parametrize("text, skill", [
    ("Portfolio, Programme & PMO: Programme Management", PM),
    ("Organisational Design for the operating model", ORG),
    ("Cloud Cost Optimisation programme", OPT),
    ("Data Modelling and warehousing", DM),
    ("Behavioural Analytics platform", BEH),
    ("Owned Service Catalogue Management", CAT),
])
def test_british_resume_text_matches_american_catalogue(text, skill):
    assert skill in _found(text)


def test_spelling_applies_to_term_keys_both_ways():
    assert term_key("Programme Management") == term_key("Program Management") == "program management"


# --- wrapped lines ----------------------------------------------------------

def test_excerpt_joins_an_unfinished_line():
    lines = ["Certifications", "IAPP AI Governance Professional (AIGP) (In", "Progress)", "PMP"]
    assert excerpt_at(lines, 1) == "IAPP AI Governance Professional (AIGP) (In Progress)"
    lines = ["Led global AI program governance, including responsible AI frameworks and", "adoption standards."]
    assert excerpt_at(lines, 0).endswith("frameworks and adoption standards.")


def test_excerpt_does_not_join_a_finished_line_or_a_heading():
    assert excerpt_at(["Program Management", "Change Management"], 0) == "Program Management"
    assert excerpt_at(["Delivered the roadmap (2021) and", "Work Experience"], 0) == "Delivered the roadmap (2021) and"


def test_wrapped_inferred_quote_is_found_and_shown_whole():
    text = ("Summary\nSenior leader who Led\ntransformation initiatives spanning 37 brands and 32 markets.\n"
            "Work Experience\nDirector")
    records = build_evidence(
        text=text, index=ScanIndex.build(CATALOG, {}), resolver=SkillSenseResolver([]),
        llm_matches=[LLMSkillMatch(
            CM, "Change Management (inferred: Led transformation initiatives spanning 37 brands)", "EXACT")],
    )
    snippet = next(r for r in records if r.canonical_skill_id == CM).evidence[0]
    assert snippet["text"] == "Senior leader who Led transformation initiatives spanning 37 brands and 32 markets."
    assert snippet["section"] == "SUMMARY"


# --- pattern rule: AI near governance --------------------------------------

def _pattern(text):
    lines = text.splitlines()
    return pattern_hits(lines, [section_of_line(lines, i) for i in range(len(lines))], [AI_GOV_RULE])


def test_ai_near_governance_in_experience_is_a_derived_match():
    text = "Work Experience\nLed global AI program governance, including responsible AI frameworks"
    assert [h.skill_id for h in _pattern(text)] == [AIG]
    records = build_evidence(text=text, index=ScanIndex.build(CATALOG, {}), resolver=SkillSenseResolver([]),
                             llm_matches=[], patterns=[AI_GOV_RULE])
    record = next(r for r in records if r.canonical_skill_id == AIG)
    assert record.best_source == SOURCE_DERIVED and float(record.recognition) == 0.8   # never auto-confirmed
    assert record.evidence[0]["origin"] == "pattern rule 'AI near governance (Experience)'"


def test_responsible_ai_governance_matches_too():
    assert _pattern("Experience\nResponsible AI governance board chair")


@pytest.mark.parametrize("text", [
    "Work Experience\nOwned IT governance and audit",                         # regression: IT is not AI
    "Work Experience\nDelivery, IT governance, PMO and business analysis",
    "Work Experience\nused ai tools; data governance office",                 # lower-case "ai" isn't the anchor
    "Work Experience\nAI roadmap for the retail business across five markets with board-level governance",
    "R E C O G N I T I O N\nISACA Chennai - AI Governance",                   # not an Experience section
    "Core Competencies\nAI program governance",
])
def test_pattern_does_not_fire(text):
    assert _pattern(text) == []


def test_it_governance_never_matches_ai_governance_from_any_source():
    blocked = resolve_blocklist([{"term": "IT governance", "skill": "AI Governance"}], CATALOG)
    records = build_evidence(
        text="Work Experience\nDelivery, IT governance, PMO and business analysis",
        index=ScanIndex.build(CATALOG, {}), resolver=SkillSenseResolver([]),
        llm_matches=[LLMSkillMatch(AIG, "IT governance", "FUZZY")], blocked=blocked, patterns=[AI_GOV_RULE],
    )
    assert AIG not in {r.canonical_skill_id for r in records}


# --- job-title pattern rule (synthetic programme-lead fixture) ---------------

import json  # noqa: E402
from pathlib import Path  # noqa: E402

from app.services.skills.core_skill_confirmation_service import (  # noqa: E402
    CoreSkillSpec, auto_confirm_evidence,
)
from app.services.skills.resume_skill_scanner import title_pattern_hits  # noqa: E402

PROFILE = json.loads((Path(__file__).parents[2] / "fixtures" / "scoring_v2" / "synthetic_programme_lead.json").read_text("utf-8"))
PROGRAM_TITLE_RULE = PatternRule(PM, "Program Head/Manager/Director (job title)", ("Program",),
                                 ("Head", "Manager", "Director"), 0, frozenset({"EXPERIENCE"}), False, "JOB_TITLE")


def _profile_records():
    return build_evidence(
        text="\n".join(PROFILE["text"]), index=ScanIndex.build(CATALOG, {}), resolver=SkillSenseResolver([]),
        llm_matches=[], patterns=[AI_GOV_RULE, PROGRAM_TITLE_RULE], job_titles=PROFILE["work_experience_titles"],
    )


def test_programme_manager_title_is_a_derived_program_management_match():
    hits = title_pattern_hits(PROFILE["work_experience_titles"], [PROGRAM_TITLE_RULE])
    assert {h.title for h in hits} == {"Senior Programme Manager, Advisory",
                                       "Quality CoE Lead / Programme Manager"}
    record = next(r for r in _profile_records() if r.canonical_skill_id == PM)
    assert record.best_source == SOURCE_DERIVED and float(record.recognition) == 0.8
    assert record.evidence[0]["section"] == "EXPERIENCE"
    assert record.evidence[0]["origin"].startswith("job title pattern rule")


def test_title_match_goes_to_the_vote_never_auto_confirmed():
    record = next(r for r in _profile_records() if r.canonical_skill_id == PM)
    assert auto_confirm_evidence(CoreSkillSpec(PM, "Program Management"), [record]) is None


@pytest.mark.parametrize("title", [
    "General Manager, O2C Transformation", "Director, Operations Excellence", "Project Manager",
    "Programmer", "Head of Program Office", "Product Manager",
])
def test_title_rule_needs_program_right_before_head_manager_director(title):
    assert title_pattern_hits([title], [PROGRAM_TITLE_RULE]) == []


@pytest.mark.parametrize("title", ["Program Director", "Programme Head", "Senior Program Manager - TurboTax",
                                   "program manager"])
def test_title_rule_variants(title):
    assert len(title_pattern_hits([title], [PROGRAM_TITLE_RULE])) == 1


def test_target_roles_line_is_not_a_held_title():
    records = build_evidence(text="\n".join(PROFILE["text"]), index=ScanIndex.build(CATALOG, {}),
                             resolver=SkillSenseResolver([]), llm_matches=[], patterns=[PROGRAM_TITLE_RULE],
                             job_titles=[])
    assert PM not in {r.canonical_skill_id for r in records}


def test_profile_ai_governance_is_auto_confirmed_from_profile_not_recognition():
    record = next(r for r in _profile_records() if r.canonical_skill_id == AIG)
    item = auto_confirm_evidence(CoreSkillSpec(AIG, "AI Governance"), [record])
    assert item is not None and item["section"] == "SUMMARY" and item["text"].startswith("AI GRC & security")


def test_profile_it_governance_line_adds_nothing():
    texts = [e["text"] for r in _profile_records() for e in r.evidence]
    assert not any("IT governance" in t for t in texts)
