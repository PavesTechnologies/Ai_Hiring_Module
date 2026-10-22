"""Hard-negative fixes: qualifier lines, work verbs, role labels, in-progress certifications, vote context."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.campaign.scoring_v2_service import _paraphrase_targets_for, job_context
from app.services.skills.certification_mapping_service import rule_from
from app.services.skills.core_skill_confirmation_service import (
    CONFIRMATION_PROMPT, CoreSkillConfirmationService, CoreSkillSpec, JobContext, _Ask, evidence_hash,
)
from app.services.skills.evidence_rules import DEFAULT_RULES, EvidenceRules, role_for_line, roles_of
from app.services.skills.paraphrase_proposal_service import candidate_lines
from app.services.skills.resume_skill_scanner import ScanIndex
from app.services.skills.skill_evidence_service import LLMSkillMatch, build_evidence
from app.services.skills.skill_sense_resolver import SkillSenseResolver

PY, SQL, GIT, ML, INC, SNOW, CHG = (uuid4() for _ in range(7))
CATALOG = [
    SimpleNamespace(id=PY, canonical_name="Python", aliases=[]),
    SimpleNamespace(id=SQL, canonical_name="SQL", aliases=[]),
    SimpleNamespace(id=GIT, canonical_name="Git", aliases=[]),
    SimpleNamespace(id=ML, canonical_name="Machine Learning", aliases=[]),
    SimpleNamespace(id=INC, canonical_name="Incident Management", aliases=[]),
    SimpleNamespace(id=SNOW, canonical_name="ServiceNow ITSM", aliases=[]),
    SimpleNamespace(id=CHG, canonical_name="Change Management", aliases=[]),
]
INDEX = ScanIndex.build(CATALOG, {})
RULES = EvidenceRules.from_config(None, catalog_terms=INDEX.entries.keys())


def _evidence(text, *, llm=(), roles=None, certifications=None, certification_rules=None):
    return {r.canonical_skill_id: r for r in build_evidence(
        text=text, index=INDEX, resolver=SkillSenseResolver([]), llm_matches=list(llm), rules=RULES, roles=roles,
        certifications=certifications, certification_rules=certification_rules)}


# --- qualifier lines ---------------------------------------------------------

@pytest.mark.parametrize("line", [
    "Currently learning: Python, SQL, Git, REST APIs",          # H10
    "Familiar with Python and SQL",
    "Basic knowledge of Git",
    "Exposure to Incident Management",
    "Interested in Machine Learning and Python",
    "Aspiring Python developer",
    "Learning Python on weekends",
])
def test_qualifier_lines(line):
    assert RULES.qualified(line)


@pytest.mark.parametrize("line", [
    "Machine Learning, Python, SQL",                            # the skill name holds "learning"
    "Built Machine Learning pipelines in Python",
    "Python, SQL, REST APIs, Git",
])
def test_not_qualifier_lines(line):
    assert not RULES.qualified(line)


def test_qualifiers_come_from_config():
    rules = EvidenceRules.from_config({"qualifiers": ["self-taught"]})
    assert rules.qualified("Self-taught: Python") and not rules.qualified("Currently learning: Python")


def test_a_currently_learning_line_is_no_evidence_from_any_source():
    text = "Skills\nProficient: MS Office, customer communication\nCurrently learning: Python, SQL, Git, REST APIs"
    found = _evidence(text, llm=[LLMSkillMatch(PY, "Python", "EXACT"), LLMSkillMatch(SQL, "SQL", "EXACT")])
    assert set(found) == set()


def test_machine_learning_lines_still_count():
    found = _evidence("Skills\nMachine Learning, Python, SQL")
    assert {ML, PY, SQL} <= set(found)


def test_paraphrase_never_proposes_a_qualifier_line():
    text = "Experience\nCurrently learning to automate deployments with Python scripts\nBuilt REST APIs in Flask for billing"
    lines = [line for _, line, _ in candidate_lines(text, {"sections": ["EXPERIENCE"]}, RULES.qualified)]
    assert lines == ["Built REST APIs in Flask for billing"]


# --- work verbs ----------------------------------------------------------------

@pytest.mark.parametrize("line, works", [
    ("Configured incident and change modules on ServiceNow", True),
    ("Built REST APIs in Java with Spring Boot", True),
    ("Used Git with pull requests and code reviews", True),
    ("Customised the ServiceNow ITSM catalogue", True),                       # British spelling
    ("Sold ServiceNow ITSM implementation services to mid-market clients", False),   # H6
    ("Ran product demos of incident and change modules", False),
    ("Supported and configured ServiceNow incident forms", False),             # a not-work verb too
    ("ServiceNow ITSM, Incident Management, Change Management", False),        # a list, no verb
])
def test_does_the_work(line, works):
    assert DEFAULT_RULES.does_the_work(line) is works


# --- role labels -----------------------------------------------------------------

H2_TEXT = "\n".join([
    "Siddharth Bose", "Experience",
    "Senior Salesforce Consultant — Cloudharbor Partners · Jan 2023 – present",
    "Configured case management and incident escalation rules in Salesforce Service Cloud",
    "Salesforce Developer — Cloudharbor Partners · Jul 2020 – Dec 2022",
    "Wrote Apex triggers and Lightning components",
])
H2_ROLES = roles_of({"work_experience": [
    {"title": "Senior Salesforce Consultant", "company": "Cloudharbor Partners",
     "description": "Configured case management and incident escalation rules in Salesforce Service Cloud, "
                    "Deployed changes to production using change sets"},
    {"title": "Salesforce Developer", "company": "Cloudharbor Partners", "description": None},
]})


def test_a_line_gets_its_role_from_the_description_or_the_header_above():
    lines = H2_TEXT.splitlines()
    assert role_for_line(lines, 3, H2_ROLES) == "Senior Salesforce Consultant, Cloudharbor Partners"
    assert role_for_line(lines, 5, H2_ROLES) == "Salesforce Developer, Cloudharbor Partners"   # header above
    assert role_for_line(lines, 0, H2_ROLES) is None


def test_experience_excerpts_carry_their_role():
    found = _evidence(H2_TEXT + "\nOwned Incident Management reporting", roles=H2_ROLES)
    item = found[INC].evidence[0]
    assert item["section"] == "EXPERIENCE" and item["role"] == "Salesforce Developer, Cloudharbor Partners"


# --- certifications: the resume's own text ------------------------------------------

def test_an_appearing_certification_is_not_mapped_even_when_the_parse_dropped_it():
    # H5: parsed_json kept "ServiceNow Certified System Administrator" without "(appearing, Dec 2026)".
    text = ("Certifications\nITIL 4 Foundation; CCNP Enterprise; ServiceNow Certified System Administrator "
            "(appearing, Dec 2026)")
    rules = [rule_from("ITIL 4 Foundation", [], [(CHG, None), (INC, None)]),
             rule_from("ServiceNow CSA", ["ServiceNow Certified System Administrator"], [(SNOW, None)])]
    found = _evidence(text, certifications=["ITIL 4 Foundation", "CCNP Enterprise",
                                            "ServiceNow Certified System Administrator"], certification_rules=rules)
    assert SNOW not in found and {CHG, INC} <= set(found)       # ITIL on the same line still counts


# --- vote context ------------------------------------------------------------------

def test_the_prompt_names_the_job_its_domain_and_the_domain_rule():
    prompt = CONFIRMATION_PROMPT.format(job=JobContext("ServiceNow ITSM Consultant", "IT operations").render(),
                                        items="")
    assert "The job: ServiceNow ITSM Consultant (role domain: IT operations)." in prompt
    assert "equivalent work in a different domain is not the skill" in prompt
    assert "a different tool in the same domain can be" in prompt


def test_excerpts_are_labelled_with_their_role_and_the_vote_sees_it():
    class Provider:
        calls = []

        def generate_json(self, prompt, response_schema, *, deterministic=False):
            self.calls.append(prompt)
            return {"items": [{"number": 1, "present": False, "quote": ""}]}

    provider = Provider()
    row = {"canonical_skill_id": INC, "sense": None, "sources": ["DERIVED"], "evidence": [
        {"text": "Configured case management and incident escalation rules", "source": "DERIVED", "origin": "x",
         "section": "EXPERIENCE", "role": "Senior Salesforce Consultant, Cloudharbor Partners"}]}
    CoreSkillConfirmationService(provider).confirm(
        resume_id=uuid4(), jd_id=uuid4(), core_skills=[CoreSkillSpec(INC, "Incident Management")],
        evidence_rows=[row], text_hash="h", job=JobContext("ServiceNow ITSM Consultant", "IT operations"))
    assert '[Experience - Senior Salesforce Consultant, Cloudharbor Partners] "Configured case management' \
           in provider.calls[0]
    assert "The job: ServiceNow ITSM Consultant (role domain: IT operations)." in provider.calls[0]


def test_a_cached_answer_is_asked_again_when_the_job_context_changes():
    spec = CoreSkillSpec(INC, "Incident Management")
    a = _Ask(1, spec, [("Experience", "line")], ["SCAN"], JobContext("ServiceNow ITSM Consultant", None))
    b = _Ask(1, spec, [("Experience", "line")], ["SCAN"], JobContext("ServiceNow ITSM Consultant", "IT operations"))
    assert evidence_hash(a) != evidence_hash(b)


def test_job_context_uses_the_calibrated_domain_else_the_suggested_one():
    calibrated = SimpleNamespace(title="AI transformation lead", role_domain="BUSINESS")
    assert job_context(calibrated, []).role_domain == "Business"
    unset = SimpleNamespace(title="ServiceNow ITSM Consultant", role_domain=None)
    assert job_context(unset, ["Incident Management"]).role_domain == "IT operations"


# --- rejected certification evidence keeps the skill open ------------------------------

def _spec(skill_id, name):
    return CoreSkillSpec(skill_id, name)


def _row(skill_id, origin, section):
    return SimpleNamespace(canonical_skill_id=skill_id, evidence=[{"text": "x", "origin": origin, "section": section}])


def test_a_skill_rejected_on_certification_evidence_alone_stays_a_paraphrase_target():
    specs = [_spec(INC, "Incident Management"), _spec(CHG, "Change Management")]
    confirmations = {str(INC): {"status": "REJECTED"}, str(CHG): {"status": "REJECTED"}}
    rows = [_row(INC, "certification 'ITIL 4 Foundation'", "CERTIFICATIONS"),
            _row(CHG, "certification 'ITIL 4 Foundation'", "CERTIFICATIONS"),
            _row(CHG, "text-scan", "EXPERIENCE")]                 # Change also had a work line: decided
    targets = _paraphrase_targets_for(specs, confirmations, {}, rows)
    assert [t.name for t in targets] == ["Incident Management"]


# --- stored evidence from older detection rules is detected again --------------------------

@pytest.mark.parametrize("stored_version, redetected", [("2026.10.3", True), (None, False)])
def test_stale_stored_evidence_is_detected_again(stored_version, redetected):
    from unittest.mock import MagicMock, patch
    from app.services.campaign import scoring_v2_service as svc
    from app.services.skills.resume_skill_scanner import DETECTION_VERSION
    service = svc.ScoringV2Service(MagicMock(), MagicMock(), MagicMock(), provider_resolver=None)
    service.detection_repository = MagicMock()
    service.evidence_service = MagicMock()
    service.load_config = MagicMock(return_value={})
    row = SimpleNamespace(canonical_skill_id=PY, sense=None, sources=["SCAN"], best_source="SCAN", match_tier="SCAN",
                          recognition=1.0, evidence=[{"text": "line"}], text_hash="h",
                          detection_version=stored_version or DETECTION_VERSION)
    service.detection_repository.list_evidence.return_value = [row]
    service.db.query.return_value.filter.return_value.all.return_value = []

    class Stop(Exception):
        pass

    with patch.object(svc, "detect_stored_resume", return_value=([row], "text")) as detect,             patch.object(svc, "jd_core_skill_specs", side_effect=Stop):
        with pytest.raises(Stop):
            service.score(campaign=MagicMock(), campaign_candidate=MagicMock(), job_description=MagicMock(),
                          resume=MagicMock(), experience_result=None, education_result=None, domain_result=None,
                          acted_on=False, persist=False)
    assert detect.called is redetected

