from app.services.campaign.domain_capability_matching_service import DomainCapabilityMatchingService

SPM_JD_JSON = {
    "domain_capabilities": {
        "required": [
            "Demand Management", "Portfolio Planning", "Portfolio Hierarchy", "Investment Funding",
            "Financial Planning", "Resource Management", "Data Modelling",
        ],
        "preferred": ["Application Portfolio Management", "Enterprise Architecture"],
    },
}

SPM_RESUME_JSON = {
    "summary": "ServiceNow developer with 7 years of experience.",
    "work_experience": [
        {
            "title": "Senior ServiceNow Developer",
            "description": (
                "Implemented SPM: demand intake and approval flows, portfolio setup, cost plans and "
                "funding allocation, resource plans; built Business Rules, Script Includes, Flow Designer"
            ),
        },
    ],
}


def _by_name(results: list[dict]) -> dict:
    return {item["capability"]: item for item in results}


def test_example_resume_gets_partial_credit_for_differently_worded_capabilities():
    result = DomainCapabilityMatchingService().evaluate(SPM_JD_JSON, SPM_RESUME_JSON)
    required = _by_name(result["required"])

    assert result["applicable"] is True
    assert result["passed"] is True
    assert required["Demand Management"]["score"] == 0.5
    assert required["Investment Funding"]["score"] == 0.5
    assert required["Resource Management"]["score"] == 0.5
    assert required["Portfolio Planning"]["score"] == 1.0  # "portfolio setup, cost plans"
    assert required["Data Modelling"]["score"] == 0.0
    assert required["Demand Management"]["evidence"].startswith("Implemented SPM")
    assert required["Data Modelling"]["evidence"] is None
    assert 0 < result["score"] < 100


def test_exact_wording_scores_full_marks():
    resume = {"work_experience": [{"description": "Owned Demand Management and Resource Management rollout."}]}
    jd = {"domain_capabilities": {"required": ["Demand Management", "Resource Management"], "preferred": []}}

    result = DomainCapabilityMatchingService().evaluate(jd, resume)

    assert result["score"] == 100.0


def test_words_split_across_sentences_do_not_combine():
    resume = {"work_experience": [{"description": "Managed a team. Maintained resource calendars."}]}
    jd = {"domain_capabilities": {"required": ["Resource Management"], "preferred": []}}

    result = DomainCapabilityMatchingService().evaluate(jd, resume)

    assert result["required"][0]["score"] == 0.5


def test_parenthetical_acronym_matches_as_whole_word():
    resume = {"projects": [{"name": "APM rollout", "description": "Rationalised 300 applications"}]}
    jd = {"domain_capabilities": {"required": [], "preferred": ["Application Portfolio Management (APM)"]}}

    result = DomainCapabilityMatchingService().evaluate(jd, resume)

    assert result["preferred"][0]["score"] == 1.0


def test_preferred_capabilities_weigh_half_of_required():
    resume = {"summary": "Led demand management."}
    jd = {"domain_capabilities": {"required": ["Demand Management"], "preferred": ["Enterprise Architecture"]}}

    result = DomainCapabilityMatchingService().evaluate(jd, resume)

    assert result["score"] == round(1.0 / 1.5 * 100, 2)


def test_jd_without_domain_capabilities_is_skipped():
    result = DomainCapabilityMatchingService().evaluate({"required_skills": {"core": ["Java"]}}, SPM_RESUME_JSON)

    assert result["applicable"] is False
    assert result["skipped"] is True
    assert result["score"] is None


def test_resume_without_text_is_data_missing():
    result = DomainCapabilityMatchingService().evaluate(SPM_JD_JSON, {"skills": ["Java"]})

    assert result["applicable"] is False
    assert result["data_missing"] is True
