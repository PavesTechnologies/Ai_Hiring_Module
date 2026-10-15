"""
Phase 3 Step 5: certification -> skill mapping (scoring v2).

A held certification matching a mapped name or variant gives DERIVED
evidence for each mapped skill, in the mapped sense, always sent to the
3-vote confirmation. A certification still being worked towards never
counts. Bare "ITIL" (no level) maps to nothing.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from app.exception_handler.exceptions import ConflictError, NotFoundError, UnprocessableError
from app.services.skills.certification_mapping_service import (
    CertificationMappingService, certification_words, is_in_progress, match_certifications, rule_from,
)
from app.services.skills.core_skill_confirmation_service import CoreSkillConfirmationService, CoreSkillSpec
from app.services.skills.resume_skill_scanner import ScanIndex
from app.services.skills.skill_evidence_service import SOURCE_DERIVED, build_evidence
from app.services.skills.skill_sense_resolver import SenseDef, SkillSenseResolver

INC, CM, PRB, SNOW, PGM = (uuid4() for _ in range(5))
ITIL_SKILLS = [(INC, None), (CM, "ITSM"), (PRB, None)]
RULES = [
    rule_from("ITIL 4 Foundation", ["ITIL 4 Foundation", "ITIL v4 Foundation"], ITIL_SKILLS),
    rule_from("ITIL Foundation (v3 / unversioned)", ["ITIL Foundation", "ITIL v3 Foundation", "ITIL V3"], ITIL_SKILLS),
    rule_from("ServiceNow CSA", ["ServiceNow CSA", "ServiceNow Certified System Administrator"], [(SNOW, None)]),
    rule_from("PMP", ["PMP", "Project Management Professional"], [(PGM, None)]),
]
SENSES = [SenseDef(CM, "ITSM", "IT service change", "", ("cab", "itil")),
          SenseDef(CM, "ORGANISATIONAL", "People side", "", ("adoption", "stakeholders"), is_default=True)]


def _matched(certification):
    return [m.rule.name for m in match_certifications([certification], RULES)]


# --- names ------------------------------------------------------------------------

def test_names_are_normalised():
    assert certification_words("ITIL® v4 Foundation") == ("itil", "4", "foundation")
    assert certification_words("ITIL4 Foundation") == ("itil", "4", "foundation")
    assert certification_words("ServiceNow CIS-ITSM") == ("servicenow", "cis", "itsm")


@pytest.mark.parametrize("certification, expected", [
    ("ITIL 4 Foundation", ["ITIL 4 Foundation"]),
    ("ITIL v4 Foundation", ["ITIL 4 Foundation"]),
    ("ITIL® 4 Foundation Certificate", ["ITIL 4 Foundation"]),
    ("ITIL v3 Foundation", ["ITIL Foundation (v3 / unversioned)"]),
    ("ITIL Foundation", ["ITIL Foundation (v3 / unversioned)"]),
    ("ITIL V3 Expert", ["ITIL Foundation (v3 / unversioned)"]),
    ("ITIL", []),                                     # bare ITIL: no level, unmapped
    ("ITIL Intermediate Certification", []),
    ("ServiceNow Certified System Administrator (CSA)", ["ServiceNow CSA"]),
    ("Project Management Professional (PMP) – PMI (2026)", ["PMP"]),
    ("Certified Scrum Master", []),
])
def test_names_and_variants_match_by_contiguous_words(certification, expected):
    assert _matched(certification) == expected


# --- in progress ------------------------------------------------------------------

@pytest.mark.parametrize("certification", [
    "IAPP AI Governance Professional (AIGP) (In Progress)",
    "PMP - in-progress", "PMP (pursuing)", "PMP - ongoing", "PMP underway",
    "PMP (appearing)", "PMP exam scheduled",
    "PMP (expected 2027)", "PMP - expected Dec 2026", "PMP (Expected: 03/2027)", "PMP expected Sept. 2026",
])
def test_a_certification_being_worked_towards_never_counts(certification):
    assert is_in_progress(certification) and _matched(certification) == []


@pytest.mark.parametrize("certification", [
    "Project Management Professional (PMP) – PMI (2026)",     # a year alone is when it was earned
    "PMP (exceeded expected targets)",                          # "expected" without a date
])
def test_an_earned_certification_counts(certification):
    assert not is_in_progress(certification) and _matched(certification) == ["PMP"]


# --- evidence ---------------------------------------------------------------------

D3_TEXT = "\n".join([
    "ServiceNow administrator with 4.5 years of experience.",
    "Experience",
    "Support the change process and prepare changes for approval meetings",
    "Certifications",
    "ITIL 4 Foundation; ServiceNow Certified System Administrator (CSA)",
])


def _evidence(text, certifications):
    return {r.canonical_skill_id: r for r in build_evidence(
        text=text, index=ScanIndex.build([], {}), resolver=SkillSenseResolver(SENSES), llm_matches=[],
        certifications=certifications, certification_rules=RULES)}


def test_phase3_d3_certifications_give_derived_evidence_in_the_mapped_sense():
    records = _evidence(D3_TEXT, ["ITIL 4 Foundation", "ServiceNow Certified System Administrator (CSA)"])
    assert set(records) == {INC, CM, PRB, SNOW}
    for record in records.values():
        assert record.sources == [SOURCE_DERIVED] and record.best_source == SOURCE_DERIVED
        assert record.evidence[0]["text"].startswith("ITIL 4 Foundation; ServiceNow")    # the resume's own line
        assert record.evidence[0]["section"] == "CERTIFICATIONS"
    assert (records[CM].sense, records[CM].sense_resolved) == ("ITSM", True)
    assert records[INC].evidence[0]["origin"] == "certification 'ITIL 4 Foundation'"
    assert records[SNOW].evidence[0]["origin"] == "certification 'ServiceNow CSA'"


def test_an_in_progress_certification_gives_no_evidence():
    assert _evidence("Certifications\nPMP (In Progress)", ["PMP (In Progress)"]) == {}


def test_a_certifications_sense_does_not_decide_the_resumes_own_mentions():
    # The ITIL certificate says ITSM; the bare "Change Management" skill line says nothing,
    # so it stays unresolved - not turned ITSM - exactly as without the certificate.
    text = "Skills\nChange Management\n\n\nCertifications\nITIL 4 Foundation"
    index = ScanIndex.build([SimpleNamespace(id=CM, canonical_name="Change Management", aliases=[])], {})
    records = build_evidence(text=text, index=index, resolver=SkillSenseResolver(SENSES), llm_matches=[],
                             certifications=["ITIL 4 Foundation"], certification_rules=RULES)
    senses = sorted((r.sense or "-", r.sense_resolved) for r in records if r.canonical_skill_id == CM)
    assert senses == [("-", False), ("ITSM", True)]


def test_certification_evidence_is_always_voted_never_auto_confirmed():
    provider = MagicMock()
    provider.provider_name, provider.model = "GOOGLE", "m"
    provider.last_model_used, provider.last_used_fallback = None, False
    provider.generate_json.return_value = {"items": [{"number": 1, "present": True, "quote": "ITIL 4 Foundation"}]}
    record = _evidence(D3_TEXT, ["ITIL 4 Foundation"])[INC]
    row = {"canonical_skill_id": INC, "sense": None, "sources": record.sources, "evidence": record.evidence}
    outcome = CoreSkillConfirmationService(provider, None).confirm(
        resume_id=uuid4(), jd_id=uuid4(), core_skills=[CoreSkillSpec(INC, "Incident Management")],
        evidence_rows=[row], text_hash="d3")
    assert provider.generate_json.call_count == 3                  # the 3-vote check, not auto-confirmed
    assert outcome.results[0]["status"] == "CONFIRMED"


# --- editing (HR_ADMIN) -------------------------------------------------------------

def _service(existing=None, skill=None):
    repository, skills, audit = MagicMock(), MagicMock(), MagicMock()
    repository.find_by_name.return_value = existing
    repository.add.side_effect = lambda c: (setattr(c, "id", uuid4()), c)[1]
    skills.get_skill_by_id.return_value = skill
    return CertificationMappingService(repository, skills, lambda: SENSES, audit), repository, audit


def test_adding_a_certification_keeps_usable_variants_and_is_audited():
    service, repository, audit = _service()
    certification = service.add_certification(name=" ITIL 4 Foundation ", variants=["ITIL v4 Foundation", " ", "!!"],
                                              note=None, actor_id="hr-1", actor_role="HR_ADMIN")
    assert certification.name == "ITIL 4 Foundation" and certification.name_variants == ["ITIL v4 Foundation"]
    assert audit.log.call_args.kwargs["action_type"].value == "CERTIFICATION_ADDED"
    repository.commit.assert_called_once()


def test_a_certification_name_already_there_is_a_conflict():
    service, repository, audit = _service(existing=object())
    with pytest.raises(ConflictError):
        service.add_certification(name="PMP", variants=[], note=None, actor_id="hr-1", actor_role="HR_ADMIN")
    repository.rollback.assert_called_once()


def test_mapping_a_skill_checks_the_meaning_and_duplicates():
    cert = SimpleNamespace(id=uuid4(), name="ITIL 4 Foundation", skills=[])
    service, repository, audit = _service(skill=SimpleNamespace(id=CM, canonical_name="Change Management"))
    repository.get.return_value = cert
    with pytest.raises(UnprocessableError):
        service.add_skill(cert.id, canonical_skill_id=CM, sense_key="FINANCE", actor_id="hr-1", actor_role="HR_ADMIN")
    service.add_skill(cert.id, canonical_skill_id=CM, sense_key="ITSM", actor_id="hr-1", actor_role="HR_ADMIN")
    assert repository.add_skill.call_args.args[0].sense_key == "ITSM"
    assert audit.log.call_args.kwargs["action_type"].value == "CERTIFICATION_SKILL_ADDED"
    cert.skills = [SimpleNamespace(canonical_skill_id=CM, sense_key="ITSM")]
    with pytest.raises(ConflictError):
        service.add_skill(cert.id, canonical_skill_id=CM, sense_key="ITSM", actor_id="hr-1", actor_role="HR_ADMIN")


def test_removing_a_skill_or_a_certification_is_audited():
    cert = SimpleNamespace(id=uuid4(), name="PMP", name_variants=["PMP"],
                           skills=[SimpleNamespace(canonical_skill_id=PGM, sense_key=None)])
    service, repository, audit = _service(skill=SimpleNamespace(id=PGM, canonical_name="Program Management"))
    repository.get.return_value = cert
    service.remove_skill(cert.id, PGM, actor_id="hr-1", actor_role="HR_ADMIN")
    assert audit.log.call_args.kwargs["action_type"].value == "CERTIFICATION_SKILL_REMOVED"
    with pytest.raises(NotFoundError):
        service.remove_skill(cert.id, uuid4(), actor_id="hr-1", actor_role="HR_ADMIN")
    service.remove_certification(cert.id, actor_id="hr-1", actor_role="HR_ADMIN")
    assert audit.log.call_args.kwargs["action_type"].value == "CERTIFICATION_REMOVED"
    repository.get.return_value = None
    with pytest.raises(NotFoundError):
        service.remove_certification(uuid4(), actor_id="hr-1", actor_role="HR_ADMIN")
