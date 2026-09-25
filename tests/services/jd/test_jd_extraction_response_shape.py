import ast
import copy
import json
from pathlib import Path

from app.schemas.ai.jd_extraction_response import (
    JDExtractionGenerationSchema,
    JDExtractionResponse,
    JDSkillSpec,
)


def _seed_prompt_constants() -> dict[str, str]:
    # Parsed, not imported: importing seed_prompt_templates runs the seed against the database.
    source = Path(__file__).resolve().parents[3] / "app" / "seeds" / "seed_prompt_templates.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    return {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


_PROMPTS = _seed_prompt_constants()
_JD_PARSE_TEXT = _PROMPTS["_JD_PARSE_TEXT"]
_RESUME_PARSE_TEXT = _PROMPTS["_RESUME_PARSE_TEXT"]

SPM_RESPONSIBILITIES = [
    "Configure and develop ServiceNow SPM solutions",
    "Implement SPM capabilities around demand, projects, portfolios, investments, resources, and financial planning",
    "Configure portfolio hierarchy and associated data structures",
    "Develop and customize SPM workflows and business logic",
    "Build Business Rules, Script Includes, Client Scripts, and Flow Designer solutions",
    "Support integration and data-migration activities involving SPM",
    "Troubleshoot technical and functional issues",
    "Work with architects and functional consultants on solution implementation",
    "Participate in technical design discussions and provide implementation recommendations",
    "Follow ServiceNow development and configuration best practices",
]

# The exact extracted_json expected for the ServiceNow SPM Senior Developer JD.
SPM_EXPECTED_EXTRACTED_JSON = {
    "required_skills": {
        "core": ["ServiceNow SPM", "ServiceNow", "JavaScript", "Glide APIs"],
        "supporting": [
            "Business Rules", "Script Includes", "Client Scripts", "UI Policies", "UI Actions",
            "Flow Designer", "CSDM",
        ],
    },
    "preferred_skills": [
        "Strategic Planning Workspace", "IntegrationHub", "REST APIs", "SOAP APIs", "Import Sets",
        "Transform Maps", "UI Builder",
    ],
    "aliases": {"ServiceNow SPM": ["ITBM", "PPM"]},
    "domain_capabilities": {
        "required": [
            "Demand Management", "Portfolio Planning", "Portfolio Hierarchy", "Investment Funding",
            "Financial Planning", "Resource Management", "Data Modelling",
        ],
        "preferred": ["Application Portfolio Management", "Enterprise Architecture"],
    },
    "soft_skills": [],
    "responsibilities": SPM_RESPONSIBILITIES,
    "certifications": ["CIS-SPM", "CSA", "CAD"],
    "experience": {"min_experience_years": 6, "max_experience_years": 10},
    "education": {
        "degree": None,
        "field": None,
        "degree_level": "UNKNOWN",
        "field_normalized": "UNKNOWN",
        "related_field_allowed": False,
    },
    "employment_type": "Full-time",
    "work_mode": "Hybrid",
    "location": "PAN India",
    "metadata": {},
}


def _llm_output_for_spm() -> dict:
    """What the LLM returns under JDExtractionGenerationSchema: aliases as a list, no metadata."""
    output = copy.deepcopy(SPM_EXPECTED_EXTRACTED_JSON)
    output["aliases"] = [{"skill": "ServiceNow SPM", "aliases": ["ITBM", "PPM"]}]
    output.pop("metadata")
    return output


def _minimal(**overrides) -> dict:
    base = {"required_skills": {"core": [], "supporting": []}, "preferred_skills": []}
    base.update(overrides)
    return base


# ---------------------------------------------------------------- SPM JD (expected output)


def test_spm_llm_output_is_stored_exactly_in_the_expected_shape():
    extraction = JDExtractionResponse.model_validate(_llm_output_for_spm())

    assert extraction.model_dump(mode="json") == SPM_EXPECTED_EXTRACTED_JSON
    # Key order and int-vs-float too, not just equality.
    assert json.dumps(extraction.model_dump(mode="json")) == json.dumps(SPM_EXPECTED_EXTRACTED_JSON)


def test_spm_llm_output_is_valid_against_the_generation_schema():
    JDExtractionGenerationSchema.model_validate(_llm_output_for_spm())


def test_stored_spm_json_round_trips_unchanged():
    extraction = JDExtractionResponse.model_validate(SPM_EXPECTED_EXTRACTED_JSON)

    assert extraction.model_dump(mode="json") == SPM_EXPECTED_EXTRACTED_JSON


def test_spm_skill_specs_carry_importance_and_aliases_for_normalization():
    extraction = JDExtractionResponse.model_validate(SPM_EXPECTED_EXTRACTED_JSON)

    required = extraction.required_skill_specs()
    assert required[0] == JDSkillSpec("ServiceNow SPM", "core", ("ITBM", "PPM"))
    assert [spec.name for spec in required if spec.importance == "core"] == [
        "ServiceNow SPM", "ServiceNow", "JavaScript", "Glide APIs",
    ]
    assert len([spec for spec in required if spec.importance == "supporting"]) == 7
    assert all(spec.importance is None for spec in extraction.preferred_skill_specs())


def test_spm_no_domain_capability_appears_in_any_skill_list():
    extraction = JDExtractionResponse.model_validate(SPM_EXPECTED_EXTRACTED_JSON)
    skills = {name.casefold() for name in extraction._all_skill_names()}
    capabilities = extraction.domain_capabilities.required + extraction.domain_capabilities.preferred

    assert not skills & {value.casefold() for value in capabilities}


# ---------------------------------------------------------------- dedupe invariants


def test_synonym_listed_as_separate_skill_is_dropped_in_favour_of_the_alias():
    extraction = JDExtractionResponse.model_validate(_minimal(
        required_skills={"core": ["ServiceNow SPM", "ITBM"], "supporting": ["PPM"]},
        aliases=[{"skill": "ServiceNow SPM", "aliases": ["ITBM", "PPM"]}],
    ))

    assert extraction.required_skills.core == ["ServiceNow SPM"]
    assert extraction.required_skills.supporting == []
    assert extraction.aliases == {"ServiceNow SPM": ["ITBM", "PPM"]}


def test_domain_capability_also_extracted_as_skill_is_kept_only_as_capability():
    extraction = JDExtractionResponse.model_validate(_minimal(
        required_skills={"core": ["ServiceNow"], "supporting": ["Demand Management"]},
        domain_capabilities={"required": ["Demand Management"], "preferred": []},
    ))

    assert extraction.required_skills.supporting == []
    assert extraction.domain_capabilities.required == ["Demand Management"]


def test_duplicate_across_required_and_preferred_keeps_required_case_insensitively():
    extraction = JDExtractionResponse.model_validate(_minimal(
        required_skills={"core": ["Java"], "supporting": ["Maven"]},
        preferred_skills=["maven", "Gradle"],
    ))

    assert extraction.preferred_skills == ["Gradle"]


def test_alias_for_unknown_skill_or_equal_to_itself_is_discarded():
    extraction = JDExtractionResponse.model_validate(_minimal(
        required_skills={"core": ["ServiceNow SPM"], "supporting": []},
        aliases=[
            {"skill": "servicenow spm", "aliases": ["ServiceNow SPM", "ITBM"]},
            {"skill": "Not A Skill", "aliases": ["X"]},
        ],
    ))

    assert extraction.aliases == {"ServiceNow SPM": ["ITBM"]}


def test_required_domain_capability_is_not_repeated_as_preferred():
    extraction = JDExtractionResponse.model_validate(_minimal(
        domain_capabilities={"required": ["Resource Management"], "preferred": ["resource management", "APM"]},
    ))

    assert extraction.domain_capabilities.preferred == ["APM"]


def test_missing_new_fields_default_to_empty():
    extraction = JDExtractionResponse.model_validate({})

    assert extraction.required_skills.core == []
    assert extraction.preferred_skills == []
    assert extraction.aliases == {}
    assert extraction.domain_capabilities.required == []
    assert extraction.domain_capabilities.preferred == []


# ---------------------------------------------------------------- legacy shape


def test_legacy_list_of_objects_shape_is_coerced():
    extraction = JDExtractionResponse.model_validate({
        "required_skills": [
            {"name": "Java", "importance": "core"},
            {"name": "Maven", "importance": "supporting"},
            {"name": "ServiceNow SPM", "importance": "core", "aliases": ["ITBM"]},
        ],
        "preferred_skills": [{"name": "Docker"}],
    })

    assert extraction.required_skills.core == ["Java", "ServiceNow SPM"]
    assert extraction.required_skills.supporting == ["Maven"]
    assert extraction.preferred_skills == ["Docker"]
    assert extraction.aliases == {"ServiceNow SPM": ["ITBM"]}


# ---------------------------------------------------------------- pure-technical JD (no regression)


def test_pure_technical_java_jd_has_no_domain_capabilities_or_aliases():
    llm_output = {
        "required_skills": {"core": ["Java", "Spring Boot"], "supporting": ["Hibernate", "Maven", "Git", "JUnit"]},
        "preferred_skills": ["Docker", "Kubernetes", "AWS"],
        "aliases": [],
        "domain_capabilities": {"required": [], "preferred": []},
        "soft_skills": ["Communication"],
        "responsibilities": ["Build REST microservices"],
        "certifications": [],
        "experience": {"min_experience_years": 3, "max_experience_years": 5},
    }

    extraction = JDExtractionResponse.model_validate(llm_output)
    dumped = extraction.model_dump(mode="json")

    assert dumped["required_skills"] == {
        "core": ["Java", "Spring Boot"], "supporting": ["Hibernate", "Maven", "Git", "JUnit"],
    }
    assert dumped["preferred_skills"] == ["Docker", "Kubernetes", "AWS"]
    assert dumped["aliases"] == {}
    assert dumped["domain_capabilities"] == {"required": [], "preferred": []}
    assert list(dumped) == list(SPM_EXPECTED_EXTRACTED_JSON)


# ---------------------------------------------------------------- prompt rules


def test_prompt_treats_parenthetical_acronym_as_same_skill():
    assert '"Object-Oriented Programming (OOP)" → "Object-Oriented Programming"' in _JD_PARSE_TEXT
    assert '["Object-Oriented Programming", "OOP"]' not in _JD_PARSE_TEXT


def test_prompt_distributes_shared_noun_when_splitting():
    assert '"REST / SOAP APIs" → ["REST APIs", "SOAP APIs"]' in _JD_PARSE_TEXT


def test_prompt_blocks_generic_standalone_fragments():
    assert 'Never extract any of these alone: "APIs", "integrations", "development", "scripting", "configuration"' in _JD_PARSE_TEXT
    assert '"ServiceNow development" → "ServiceNow"' in _JD_PARSE_TEXT


def test_prompt_routes_functional_items_to_domain_capabilities_and_tightens_core():
    assert "Technical Methodologies explicitly listed as skills" not in _JD_PARSE_TEXT
    assert "never into required_skills or preferred_skills" in _JD_PARSE_TEXT
    assert 'is NOT evidence of core' in _JD_PARSE_TEXT
    assert "Typically 2 to 5 skills" in _JD_PARSE_TEXT
    assert "put it in domain_capabilities, not in skills" in _JD_PARSE_TEXT


def test_prompt_records_aliases_instead_of_separate_skills():
    assert '"ServiceNow SPM / ITBM / PPM" → skill "ServiceNow SPM", aliases ["ITBM", "PPM"]' in _JD_PARSE_TEXT
    assert "- Map aliases." not in _JD_PARSE_TEXT


def test_resume_prompt_no_longer_splits_parenthetical_acronyms():
    assert '["Object-Oriented Programming", "OOP"]' not in _RESUME_PARSE_TEXT
    assert '"Object-Oriented Programming (OOP)" → "Object-Oriented Programming"' in _RESUME_PARSE_TEXT
