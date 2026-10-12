"""Scoring v2 skill detection: catalogue scan, distributed splitting, senses and the scan/LLM union."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.skills.compound_skill_splitter import split_compound_skill
from app.services.skills.resume_skill_scanner import (
    ORIGIN_DERIVED, ORIGIN_SCAN, POLICY_ALLOW, POLICY_EXCLUDE, POLICY_REQUIRE_CUE, ScanIndex, ScanTermRule,
    scan_text,
)
from app.services.skills.skill_evidence_service import (
    SOURCE_DERIVED, SOURCE_INFERRED, SOURCE_LLM, SOURCE_SCAN, LLMSkillMatch, build_evidence, classify_llm_match,
    resolve_blocklist,
)
from app.services.skills.skill_sense_resolver import SenseDef, SkillSenseResolver

NAMES = [
    "Change Management", "Incident Management", "Problem Management", "Program Management",
    "AI Transformation", "Digital Transformation", "AI Governance", "Governance", "Spark", "PostgreSQL", "AWS",
]
IDS = {name: uuid4() for name in NAMES + ["Go", "R", "ServiceNow SPM", "Artificial Intelligence",
                                          "Project Management", "RAG", "S3", "Computer Vision"]}


def _skill(name, aliases=()):
    return SimpleNamespace(id=IDS[name], canonical_name=name, aliases=list(aliases))


CATALOG = [_skill(n) for n in NAMES] + [
    _skill("Go", ["Golang"]),
    _skill("R"),
    _skill("ServiceNow SPM", ["SPM", "ITBM"]),
    _skill("Artificial Intelligence", ["AI"]),
    _skill("Project Management", ["PM"]),
    _skill("RAG"),
    _skill("S3"),
    _skill("Computer Vision", ["CV"]),
]

RULES = {
    "ai": ScanTermRule(POLICY_EXCLUDE),
    "pm": ScanTermRule(POLICY_EXCLUDE),
    "cv": ScanTermRule(POLICY_EXCLUDE),
    "r": ScanTermRule(POLICY_REQUIRE_CUE, ("RStudio", "tidyverse")),
    "go": ScanTermRule(POLICY_REQUIRE_CUE, ("Golang", "gRPC")),
    "spm": ScanTermRule(POLICY_REQUIRE_CUE, ("ServiceNow", "ITBM")),
    "rag": ScanTermRule(POLICY_REQUIRE_CUE, ("LLM", "retrieval")),
    "s3": ScanTermRule(POLICY_ALLOW),
}

SENSES = [
    SenseDef(IDS["Change Management"], "ITSM", "IT service change management", "ITSM change control",
             ("ITIL", "incident", "problem management", "CAB", "change request")),
    SenseDef(IDS["Change Management"], "ORGANISATIONAL", "Organisational change management", "People side",
             ("organisational", "adoption", "stakeholders", "transformation"), is_default=True),
]


@pytest.fixture
def index():
    return ScanIndex.build(CATALOG, RULES)


def _found(text, index):
    return {(hit.skill_id, hit.origin) for hit in scan_text(text, index)}


def _names(text, index):
    by_id = {v: k for k, v in IDS.items()}
    return sorted({by_id[hit.skill_id] for hit in scan_text(text, index)})


# --- regression: ambiguous / short aliases --------------------------------

def test_ambiguous_short_aliases_are_never_scanned_without_a_cue(index):
    text = (
        "I go to the office early. PM: Sam.\n"
        "AI-driven roadmap, R&D budget, SPM certified\n"
        "Attached my CV. RAG status reporting to the board."
    )
    assert _names(text, index) == []


def test_short_aliases_match_next_to_their_cue_word(index):
    assert "Go" in _names("Backend services in Go with gRPC", index)
    assert "ServiceNow SPM" in _names("ServiceNow SPM implementation (ITBM)", index)
    assert "RAG" in _names("Built RAG pipelines for LLM retrieval", index)
    assert "R" in _names("Statistical models in R and RStudio", index)


def test_cue_rule_term_matches_only_in_catalogue_casing(index):
    # "go" the verb never counts, even next to a cue word.
    assert "Go" not in _names("we go fast with gRPC", index)


def test_short_term_without_a_rule_is_not_scanned_but_allow_overrides():
    plain = ScanIndex.build([_skill("Computer Vision", ["CV"]), _skill("S3")], {})
    assert _names("CV attached. Stored files in S3.", plain) == []
    allowed = ScanIndex.build([_skill("S3")], {"s3": ScanTermRule(POLICY_ALLOW)})
    assert _names("Stored files in S3.", allowed) == ["S3"]


# --- regression: generic words, P&L ---------------------------------------

def test_governance_never_matches_ai_governance(index):
    assert _names("Governance and compliance oversight", index) == ["Governance"]
    assert _names("Led AI Governance across the group", index) == ["AI Governance"]  # longest match only


def test_p_and_l_never_splits(index):
    assert split_compound_skill("P&L") == []
    assert split_compound_skill("P&L Governance") == []
    # No fragment ("L Governance", "P Governance") and never AI Governance;
    # plain "Governance" isn't a whole list item here either.
    assert _names("P&L Governance | P&L ownership", index) == []
    assert _names("P&L, Governance", index) == ["Governance"]


# --- matching rules -------------------------------------------------------

def test_plain_single_word_matches_only_as_a_whole_list_item(index):
    assert "Spark" not in _names("Our team could spark new ideas in workshops", index)
    assert "Spark" in _names("Big data: Spark, Hive, Kafka", index)


def test_distinctive_single_words_match_anywhere(index):
    assert set(_names("Migrated the warehouse to postgresql on AWS", index)) == {"PostgreSQL", "AWS"}


def test_acronym_is_case_sensitive(index):
    assert "AWS" not in _names("the aws of a lion", index)


def test_multi_word_terms_match_in_sentences(index):
    assert "Program Management" in _names("Responsible for program management of 12 projects", index)


def test_distributed_phrase_yields_each_part_as_derived(index):
    found = _found("Change, Incident & Problem Management", index)
    assert (IDS["Change Management"], ORIGIN_DERIVED) in found
    assert (IDS["Incident Management"], ORIGIN_DERIVED) in found
    assert (IDS["Problem Management"], ORIGIN_SCAN) in found  # written literally


def test_head_word_distribution_on_competency_line(index):
    found = _found("Core Competencies: AI & Digital Transformation | Program Management", index)
    assert (IDS["AI Transformation"], ORIGIN_DERIVED) in found
    assert (IDS["Digital Transformation"], ORIGIN_SCAN) in found
    assert (IDS["Program Management"], ORIGIN_SCAN) in found


def test_scan_is_deterministic(index):
    text = "Core Competencies: AI & Digital Transformation | Change, Incident & Problem Management"
    runs = [sorted(map(str, _found(text, index))) for _ in range(3)]
    assert runs[0] == runs[1] == runs[2]


# --- senses ---------------------------------------------------------------

def _evidence(text, llm_matches=(), index=None):
    return build_evidence(text=text, index=index or ScanIndex.build(CATALOG, RULES),
                          resolver=SkillSenseResolver(SENSES), llm_matches=list(llm_matches))


def _cm(records):
    return [r for r in records if r.canonical_skill_id == IDS["Change Management"]]


def test_change_incident_problem_is_never_organisational_change_management():
    for text in (
        "Change, Incident & Problem Management",
        "Change, Incident & Problem Management | Stakeholder engagement and adoption",
        "Key skills\nChange, Incident & Problem Management\nOrganisational transformation",
    ):
        senses = {r.sense for r in _cm(_evidence(text))}
        assert senses == {"ITSM"}, text


def test_organisational_change_management_resolves_from_cues():
    records = _cm(_evidence("Led organisational change management and adoption for 3,000 staff"))
    assert [(r.sense, r.sense_resolved) for r in records] == [("ORGANISATIONAL", True)]


def test_change_management_without_cues_stays_unresolved():
    records = _cm(_evidence("Skills: Change Management"))
    assert [(r.sense, r.sense_resolved) for r in records] == [(None, False)]


def test_jd_sense_falls_back_to_title_then_assumed_default():
    resolver = SkillSenseResolver(SENSES)
    cm = IDS["Change Management"]
    assert resolver.resolve_jd_sense(cm, "Own the CAB and ITIL change request process", "Change Management").basis == "cue"
    title = resolver.resolve_jd_sense(cm, "Change management experience", "Change Management", "AI Transformation Lead")
    assert (title.sense, title.basis) == ("ORGANISATIONAL", "title")
    assumed = resolver.resolve_jd_sense(cm, "Change management experience", "Change Management", "Lead")
    assert (assumed.sense, assumed.basis) == ("ORGANISATIONAL", "assumed")


# --- union with the LLM ---------------------------------------------------

def test_llm_cannot_remove_a_scanned_skill():
    records = _evidence("Program Management | AWS", llm_matches=[])
    assert {r.canonical_skill_id for r in records} == {IDS["Program Management"], IDS["AWS"]}


def test_llm_adds_skills_and_sources_are_merged():
    records = _evidence("Program Management", llm_matches=[
        LLMSkillMatch(IDS["Program Management"], "Program Management", "EXACT"),
        LLMSkillMatch(IDS["Spark"], "Apache Spark", "FUZZY"),
    ])
    by_id = {r.canonical_skill_id: r for r in records}
    assert by_id[IDS["Program Management"]].sources == [SOURCE_SCAN, SOURCE_LLM]
    assert by_id[IDS["Program Management"]].best_source == SOURCE_SCAN
    assert float(by_id[IDS["Program Management"]].recognition) == 1.0
    assert by_id[IDS["Spark"]].best_source == SOURCE_LLM
    assert float(by_id[IDS["Spark"]].recognition) == 0.8  # fuzzy tier


def test_derived_and_inferred_get_0_8_recognition():
    records = _evidence("Change, Incident & Problem Management", llm_matches=[
        LLMSkillMatch(IDS["Program Management"], "Program Management (inferred: PMP certified programme lead)", "EXACT"),
    ])
    by_id = {r.canonical_skill_id: r for r in records}
    assert (by_id[IDS["Change Management"]].best_source, float(by_id[IDS["Change Management"]].recognition)) == (SOURCE_DERIVED, 0.8)
    assert (by_id[IDS["Program Management"]].best_source, float(by_id[IDS["Program Management"]].recognition)) == (SOURCE_INFERRED, 0.8)


def test_classify_llm_match_reads_origin_markers():
    assert classify_llm_match("Program Management", "EXACT")[0] == SOURCE_LLM
    assert classify_llm_match('Program Management (from "IT Project & Program Management")', "EXACT") == (
        SOURCE_DERIVED, "IT Project & Program Management", "llm-phrase-split")
    assert classify_llm_match("Program Management (inferred: PMP certified)", "EXACT") == (
        SOURCE_INFERRED, "PMP certified", "inferred")


# --- regression: v2 match blocklist ---------------------------------------

def test_it_governance_never_becomes_ai_governance_in_v2():
    """v1's fuzzy tier matches "IT governance" to AI Governance (ratio >= 85); v2 blocks that pair."""
    blocked = resolve_blocklist([{"term": "IT governance", "skill": "AI Governance"}], CATALOG)
    records = build_evidence(
        text="Delivery, IT governance, PMO and business analysis",
        index=ScanIndex.build(CATALOG, RULES), resolver=SkillSenseResolver(SENSES),
        llm_matches=[LLMSkillMatch(IDS["AI Governance"], "IT governance", "FUZZY")],
        blocked=blocked,
    )
    assert IDS["AI Governance"] not in {r.canonical_skill_id for r in records}


def test_blocklist_only_blocks_that_pair():
    blocked = resolve_blocklist([{"term": "IT governance", "skill": "AI Governance"},
                                 {"term": "x", "skill": "Not In Catalogue"}], CATALOG)
    assert blocked == frozenset({("it governance", IDS["AI Governance"])})
    records = build_evidence(
        text="Led AI Governance", index=ScanIndex.build(CATALOG, RULES), resolver=SkillSenseResolver(SENSES),
        llm_matches=[LLMSkillMatch(IDS["AI Governance"], "AI Governance", "EXACT")], blocked=blocked,
    )
    assert IDS["AI Governance"] in {r.canonical_skill_id for r in records}
