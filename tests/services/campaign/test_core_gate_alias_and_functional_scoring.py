from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from app.services.campaign.candidate_scoring_service import (
    GATE_SCOPE_ALL_MANDATORY,
    GATE_SCOPE_CORE,
    CandidateScoringService,
    MandatorySkillMatchType,
)
from app.services.campaign.domain_capability_matching_service import DomainCapabilityMatchingService

JD_ID = uuid4()
RESUME_ID = uuid4()

# ServiceNow SPM JD (expected extraction): 4 core + 7 supporting required skills.
SPM, SERVICENOW, JAVASCRIPT, GLIDE = uuid4(), uuid4(), uuid4(), uuid4()
BUSINESS_RULES, SCRIPT_INCLUDES, CLIENT_SCRIPTS, UI_POLICIES, UI_ACTIONS, FLOW_DESIGNER, CSDM = (
    uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(),
)
ITBM, PPM = uuid4(), uuid4()

NAMES = {
    SPM: "ServiceNow SPM", SERVICENOW: "ServiceNow", JAVASCRIPT: "JavaScript", GLIDE: "Glide APIs",
    BUSINESS_RULES: "Business Rules", SCRIPT_INCLUDES: "Script Includes", CLIENT_SCRIPTS: "Client Scripts",
    UI_POLICIES: "UI Policies", UI_ACTIONS: "UI Actions", FLOW_DESIGNER: "Flow Designer", CSDM: "CSDM",
    ITBM: "ITBM", PPM: "PPM",
}
CORE_IDS = [SPM, SERVICENOW, JAVASCRIPT, GLIDE]
SUPPORTING_IDS = [BUSINESS_RULES, SCRIPT_INCLUDES, CLIENT_SCRIPTS, UI_POLICIES, UI_ACTIONS, FLOW_DESIGNER, CSDM]

SPM_JD_JSON = {
    "domain_capabilities": {
        "required": [
            "Demand Management", "Portfolio Planning", "Portfolio Hierarchy", "Investment Funding",
            "Financial Planning", "Resource Management", "Data Modelling",
        ],
        "preferred": ["Application Portfolio Management", "Enterprise Architecture"],
    },
}
EXAMPLE_RESUME_JSON = {
    "work_experience": [{
        "title": "Senior ServiceNow Developer",
        "description": (
            "Implemented SPM: demand intake and approval flows, portfolio setup, cost plans and "
            "funding allocation, resource plans; built Business Rules, Script Includes, Flow Designer"
        ),
    }],
}


def _row(skill_id, importance, candidate_ids, alias_skill_ids=None):
    return SimpleNamespace(
        canonical_skill_id=skill_id, weight=1.0, mandatory=True,
        importance=importance, alias_skill_ids=alias_skill_ids,
        candidate_scoring_weight=1.0 if skill_id in candidate_ids else None,
        match_tier="EXACT" if skill_id in candidate_ids else None,
        confidence=1.0 if skill_id in candidate_ids else None,
    )


def _service(candidate_ids, alias_map=None, preferred_rows=None):
    alias_map = alias_map or {}
    rows = [_row(skill_id, "CORE", candidate_ids, alias_map.get(skill_id)) for skill_id in CORE_IDS]
    rows += [_row(skill_id, "SUPPORTING", candidate_ids, alias_map.get(skill_id)) for skill_id in SUPPORTING_IDS]

    skill_repository = MagicMock()
    skill_repository.get_mandatory_skill_coverage.side_effect = (
        lambda jd_id, resume_id, mandatory=True: rows if mandatory else (preferred_rows or [])
    )
    skill_repository.get_candidate_normalized_skills.return_value = [
        SimpleNamespace(canonical_skill_id=skill_id, scoring_weight=1.0, match_tier="EXACT", confidence=1.0)
        for skill_id in candidate_ids
    ]
    skill_repository.find_best_semantic_match.return_value = None

    ontology = MagicMock()
    ontology.get_skills_by_ids.side_effect = lambda ids: {
        skill_id: SimpleNamespace(
            id=skill_id, canonical_name=NAMES[skill_id], parent_skill_id=None, embedding=None, is_active=True,
        )
        for skill_id in ids if skill_id in NAMES
    }
    ontology.get_children_batch.side_effect = lambda ids: {skill_id: [] for skill_id in ids}

    config_repository = MagicMock()
    config_repository.get_configs_by_keys.return_value = {}

    campaign_candidate_repository = MagicMock()
    campaign_candidate = SimpleNamespace(id=uuid4())
    campaign_candidate_repository.get_by_id.return_value = campaign_candidate

    service = CandidateScoringService(skill_repository, ontology, config_repository, campaign_candidate_repository)
    return service, campaign_candidate


def _score(service, campaign_candidate, **kwargs):
    kwargs.setdefault("deterministic_threshold", 50.0)
    return service.calculate_and_store_score_breakdown(campaign_candidate.id, JD_ID, RESUME_ID, **kwargs)


# ---------------------------------------------------------------- acceptance: example resume passes the hard gate


def test_example_spm_resume_passes_the_hard_gate():
    # The example resume names SPM, ServiceNow, Business Rules, Script Includes,
    # Flow Designer - not JavaScript or Glide APIs, and none of the domain
    # capabilities verbatim.
    candidate_ids = {SPM, SERVICENOW, BUSINESS_RULES, SCRIPT_INCLUDES, FLOW_DESIGNER}
    service, campaign_candidate = _service(candidate_ids)
    domain_result = DomainCapabilityMatchingService().evaluate(SPM_JD_JSON, EXAMPLE_RESUME_JSON)

    breakdown = _score(
        service, campaign_candidate, required_skill_coverage_threshold=50.0,
        domain_result=domain_result, deterministic_threshold=0.0,
    )

    assert breakdown["gate_skill_scope"] == GATE_SCOPE_CORE
    assert breakdown["core_coverage_pct"] == 50.0
    assert breakdown["missing_core_skill_count"] == 2
    assert breakdown["coverage_passed"] is True
    assert breakdown["core_gap_passed"] is True
    assert breakdown["deterministic_passed"] is True
    assert breakdown["domain_capability_validation"]["passed"] is True


def test_example_resume_listing_itbm_satisfies_spm_via_alias():
    candidate_ids = {ITBM, SERVICENOW, JAVASCRIPT, GLIDE}
    service, campaign_candidate = _service(candidate_ids, alias_map={SPM: [ITBM, PPM]})

    breakdown = _score(service, campaign_candidate, required_skill_coverage_threshold=100.0)

    spm_entry = next(entry for entry in breakdown["mandatory_skills"] if entry["canonical_name"] == "ServiceNow SPM")
    assert spm_entry["match_type"] == MandatorySkillMatchType.EXACT.value
    assert spm_entry["matched_via_alias"] is True
    assert spm_entry["matched_candidate_skill_canonical_name"] == "ITBM"
    assert breakdown["core_coverage_pct"] == 100.0
    assert breakdown["coverage_passed"] is True


# ---------------------------------------------------------------- supporting skills are scored, not gated


def test_missing_supporting_skills_never_fail_the_coverage_gate():
    service, campaign_candidate = _service(set(CORE_IDS))

    breakdown = _score(service, campaign_candidate, required_skill_coverage_threshold=100.0, deterministic_threshold=0.0)

    assert breakdown["mandatory_coverage_pct"] == round(4 / 11 * 100, 2)
    assert breakdown["core_coverage_pct"] == 100.0
    assert breakdown["coverage_passed"] is True
    assert breakdown["technical_score"] == 0.0  # supporting skills carry the technical score
    assert breakdown["deterministic_passed"] is True


def test_supporting_skills_lower_the_score_which_the_threshold_still_checks():
    service, campaign_candidate = _service(set(CORE_IDS))

    breakdown = _score(service, campaign_candidate, deterministic_threshold=50.0)

    assert breakdown["coverage_passed"] is True
    assert breakdown["score_passed"] is False
    assert breakdown["deterministic_passed"] is False


def test_too_many_missing_core_skills_still_fails_the_gate():
    service, campaign_candidate = _service(set(SUPPORTING_IDS))

    breakdown = _score(service, campaign_candidate, max_missing_core_skills=3, deterministic_threshold=0.0)

    assert breakdown["missing_core_skill_count"] == 4
    assert breakdown["core_gap_passed"] is False
    assert breakdown["deterministic_passed"] is False
    assert "Missing core required skills: 4" in CandidateScoringService.build_rejection_reason(breakdown)


# ---------------------------------------------------------------- preferred skills: fixed 10% share


PREFERRED_IDS = [uuid4() for _ in range(7)]


def _preferred_rows(matched_count):
    return [
        SimpleNamespace(
            canonical_skill_id=skill_id, weight=1.0, mandatory=False, importance=None, alias_skill_ids=None,
            candidate_scoring_weight=1.0 if index < matched_count else None,
            match_tier="EXACT" if index < matched_count else None,
            confidence=1.0 if index < matched_count else None,
        )
        for index, skill_id in enumerate(PREFERRED_IDS)
    ]


def _technical_score(supporting_matched, preferred_matched):
    candidate_ids = set(CORE_IDS) | set(SUPPORTING_IDS[:supporting_matched])
    service, campaign_candidate = _service(candidate_ids, preferred_rows=_preferred_rows(preferred_matched))
    return _score(service, campaign_candidate, deterministic_threshold=0.0)["technical_score"]


def test_all_required_but_no_preferred_loses_only_the_preferred_share():
    assert _technical_score(supporting_matched=7, preferred_matched=0) == 90.0


def test_all_required_and_all_preferred_scores_full_marks():
    assert _technical_score(supporting_matched=7, preferred_matched=7) == 100.0


def test_preferred_skills_cannot_make_up_for_missing_required_skills():
    assert _technical_score(supporting_matched=3, preferred_matched=7) == round(0.9 * (3 / 7 * 100) + 10.0, 2)


def test_preferred_share_is_configurable():
    candidate_ids = set(CORE_IDS) | set(SUPPORTING_IDS)
    service, campaign_candidate = _service(candidate_ids, preferred_rows=_preferred_rows(0))
    service.config_repository.get_configs_by_keys.side_effect = (
        lambda keys: {"PREFERRED_SKILL_SHARE": "0.25"} if keys == ["PREFERRED_SKILL_SHARE"] else {}
    )

    breakdown = _score(service, campaign_candidate, deterministic_threshold=0.0)

    assert breakdown["technical_score"] == 75.0


# ---------------------------------------------------------------- domain capabilities are scored, never gated


def test_domain_capabilities_with_zero_evidence_do_not_fail_the_candidate():
    service, campaign_candidate = _service(set(CORE_IDS) | set(SUPPORTING_IDS))
    domain_result = DomainCapabilityMatchingService().evaluate(
        SPM_JD_JSON, {"summary": "Java developer building payment gateways"},
    )

    breakdown = _score(service, campaign_candidate, domain_result=domain_result, deterministic_threshold=50.0)

    assert domain_result["score"] == 0.0
    # 100 x 0.70 + 0 x 0.15, renormalized over 0.85.
    assert breakdown["deterministic_score"] == round(100.0 * 0.70 / 0.85, 2)
    assert breakdown["deterministic_passed"] is True


def test_functional_score_is_blended_with_its_weight():
    service, campaign_candidate = _service(set(CORE_IDS) | set(SUPPORTING_IDS))
    domain_result = {"applicable": True, "skipped": False, "data_missing": False, "passed": True, "score": 40.0,
                     "required": [], "preferred": []}

    breakdown = _score(
        service, campaign_candidate, domain_result=domain_result,
        score_weights={"skills": 0.6, "functional": 0.4, "experience": 0.0, "education": 0.0},
    )

    assert breakdown["functional_score"] == 40.0
    assert breakdown["deterministic_score"] == round(100.0 * 0.6 + 40.0 * 0.4, 2)


def test_legacy_unclassified_jd_keeps_gating_on_every_mandatory_skill():
    a, b = uuid4(), uuid4()
    rows = [
        SimpleNamespace(canonical_skill_id=a, weight=1.0, mandatory=True, importance=None, alias_skill_ids=None,
                        candidate_scoring_weight=1.0, match_tier="EXACT", confidence=1.0),
        SimpleNamespace(canonical_skill_id=b, weight=1.0, mandatory=True, importance=None, alias_skill_ids=None,
                        candidate_scoring_weight=None, match_tier=None, confidence=None),
    ]
    service, campaign_candidate = _service({a})
    service.skill_repository.get_mandatory_skill_coverage.side_effect = (
        lambda jd_id, resume_id, mandatory=True: rows if mandatory else []
    )

    breakdown = _score(service, campaign_candidate, required_skill_coverage_threshold=100.0)

    assert breakdown["gate_skill_scope"] == GATE_SCOPE_ALL_MANDATORY
    assert breakdown["core_coverage_pct"] == 50.0
    assert breakdown["coverage_passed"] is False
