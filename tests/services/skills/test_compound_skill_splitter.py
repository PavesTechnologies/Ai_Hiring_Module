import pytest

from app.services.skills.compound_skill_splitter import split_compound_skill

"""
Compound competency phrases in resumes ("IT Project & Program Management")
must yield their individual skills, or the matcher scores the candidate as
missing each of them.
"""


@pytest.mark.parametrize("phrase,expected", [
    ("IT Project & Program Management", ["IT Project Management", "Program Management"]),
    ("Change, Incident & Problem Management", ["Change Management", "Incident Management", "Problem Management"]),
    ("AI & Digital Transformation", ["AI Transformation", "Digital Transformation"]),
    ("Stakeholder & Client Relationship Management", ["Stakeholder Management", "Client Relationship Management"]),
    ("Risk and Compliance Management", ["Risk Management", "Compliance Management"]),
    ("Team Building & Leadership", ["Team Building", "Leadership"]),         # single-word last part: nothing carried
    ("Program Management & Governance", ["Program Management", "Governance"]),
])
def test_compound_phrases_split_with_shared_head(phrase, expected):
    assert split_compound_skill(phrase) == expected


@pytest.mark.parametrize("phrase", [
    "Program Management",          # not compound
    "CI/CD",                       # slash joins one term
    "AI/ML",
    "Front-end Development",
    "",
    None,
    "A & B & C & D & E & F & G",   # too many parts to be a skill list
    "Designed and implemented a large scale event driven platform & team",  # sentence, not a skill
])
def test_non_compound_or_unsafe_phrases_are_left_alone(phrase):
    assert split_compound_skill(phrase) == []


def test_parts_are_unique_case_insensitively():
    assert split_compound_skill("Change & change Management") == []


@pytest.mark.parametrize("phrase,expected", [
    # Unspaced "&" is part of one term - never "P Governance" / "R Management".
    ("P&L, Capex/Opex & Financial Portfolio Governance",
     ["P&L Governance", "Capex/Opex Governance", "Financial Portfolio Governance"]),
    ("R&D and Innovation Management", ["R&D Management", "Innovation Management"]),
])
def test_unspaced_ampersand_terms_stay_whole(phrase, expected):
    assert split_compound_skill(phrase) == expected


@pytest.mark.parametrize("phrase", ["M&A Integration", "AT&T Networks", "X & Y Management"])
def test_fragments_and_unsplittable_terms_are_left_alone(phrase):
    assert split_compound_skill(phrase) == []


# ---------------------------------------------------------------- generic vs specific (similarity tiers)

from app.services.skills.skill_normalization_service import is_generic_word_subset  # noqa: E402


@pytest.mark.parametrize("text,canonical", [
    ("governance", "AI Governance"),
    ("management", "Program Management"),
    ("unit testing", "Testing"),
    ("ai", "Generative AI"),
])
def test_generic_word_against_specific_skill_is_not_a_match(text, canonical):
    assert is_generic_word_subset(text, canonical)


@pytest.mark.parametrize("text,canonical", [
    ("kubernets", "Kubernetes"),          # typo - different words, fuzzy may match
    ("program managment", "Program Management"),
    ("postgres", "PostgreSQL"),
    ("ai governance", "AI Governance"),   # same words - exact tiers handle it
])
def test_typos_and_equal_names_are_not_blocked(text, canonical):
    assert not is_generic_word_subset(text, canonical)
