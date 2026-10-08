"""
Scoring v2 (Phase 1) catalogue and config data - idempotent: adds only what
is missing, never overwrites an edited row.

  python -m app.seeds.seed_scoring_v2

- platform_config LLM_MODEL_CAPABILITIES (see app/services/llm/capabilities.py;
  each rule's `source` says where its values came from)
- skill_scan_terms: ambiguous/short terms the text scan must skip or only
  match next to a cue word
- skill_senses: Change Management - ITSM vs Organisational
- platform_config SCORING_V2_CONFIG (weights/multipliers) and
  SCORING_V2_DAILY_LLM_CALL_CAP (v2 confirmation calls per day)
- skill_pattern_rules: wording patterns -> DERIVED evidence (AI near governance)
- platform_config SCORING_V2_MATCH_BLOCKLIST: known wrong matches v2 never
  accepts from any source (v1 is not affected)
"""
import json
import uuid

from app.repositories.skill_detection_repository import MATCH_BLOCKLIST_KEY
from app.services.campaign.scoring_v2 import DEFAULT_CONFIG as SCORING_V2_DEFAULT_CONFIG, SCORING_V2_CONFIG_KEY
from app.services.campaign.scoring_v2_service import DAILY_LLM_CALL_CAP_KEY, DEFAULT_DAILY_LLM_CALL_CAP
from app.services.llm.capabilities import DEFAULT_CAPABILITY_RULES, LLM_MODEL_CAPABILITIES_KEY
from app.services.skills.resume_skill_scanner import POLICY_ALLOW, POLICY_EXCLUDE, POLICY_REQUIRE_CUE, term_key

SCAN_TERMS = [
    # Named by the Phase 1 review: too ambiguous as bare words.
    ("AI", POLICY_EXCLUDE, [], "Bare 'AI' is everywhere; AI Transformation / AI Governance are matched as whole terms."),
    ("PM", POLICY_EXCLUDE, [], "Project / product / programme manager, or a time of day."),
    ("R", POLICY_REQUIRE_CUE, ["RStudio", "tidyverse", "ggplot2", "CRAN", "Shiny", "statistical computing", "dplyr"],
     "The R language, only next to R tooling."),
    ("Go", POLICY_REQUIRE_CUE, ["Golang", "goroutine", "goroutines", "gRPC", "Gin", "go modules"],
     "Go the language, never the verb."),
    ("SPM", POLICY_REQUIRE_CUE, ["ServiceNow", "ITBM", "PPM", "Strategic Portfolio Management"],
     "ServiceNow SPM, not software/statistical process management."),
    ("CV", POLICY_EXCLUDE, [], "Curriculum vitae far more often than computer vision."),
    # Other catalogue terms that are common words or acronyms with another meaning.
    ("RAG", POLICY_REQUIRE_CUE, ["LLM", "LLMs", "retrieval", "vector", "embeddings", "LangChain", "LlamaIndex", "GenAI",
                                 "generative AI"],
     "Retrieval-augmented generation, not red/amber/green status reporting."),
    ("SAM", POLICY_REQUIRE_CUE, ["AWS", "serverless", "Lambda", "CloudFormation"], "AWS SAM, not a name or 'software asset management'."),
    ("APM", POLICY_REQUIRE_CUE, ["Datadog", "New Relic", "Dynatrace", "AppDynamics", "observability", "monitoring"],
     "Application performance monitoring, not Associate Project Manager / APM certification."),
    ("ADO", POLICY_REQUIRE_CUE, ["Azure", "DevOps", "pipelines", "boards", "repos"], "Azure DevOps, not ActiveX Data Objects."),
    ("ETH", POLICY_REQUIRE_CUE, ["Ethereum", "blockchain", "web3", "Solidity", "smart contract", "smart contracts"],
     "Ethereum, not ETH Zurich."),
    ("SOL", POLICY_REQUIRE_CUE, ["Solana", "blockchain", "web3"], "Solana."),
    ("IaC", POLICY_EXCLUDE, [], "Catalogued as an alias of Terraform, but IaC is the general practice."),
    ("Air", POLICY_EXCLUDE, [], "English word."),
    ("Mix", POLICY_EXCLUDE, [], "English word (marketing/product mix)."),
    ("S3", POLICY_ALLOW, [], "Two characters, but unambiguous as written (case-sensitive)."),
]

MATCH_BLOCKLIST = [
    {"term": "IT governance", "skill": "AI Governance",
     "note": "v1 fuzzy tier matches it (ratio >= 85); IT governance is a different discipline."},
]

# The 2026-10-01 seed sent temperature to every Anthropic model; temperature
# is now off until verified. Only that untouched seeded rule is updated.
_ANTHROPIC_RULE_V1_SOURCE = "Anthropic Messages API temperature (0-1) - verify for newly released models."

PATTERN_RULES = [
    {
        "skill": "AI Governance",
        "name": "AI near governance (Experience)",
        "anchor_terms": ["AI", "responsible AI"],
        "near_terms": ["governance"],
        "max_gap_words": 3,
        "sections": ["EXPERIENCE"],
        "anchor_case_sensitive": True,
        "note": "e.g. 'led global AI program governance, including responsible AI frameworks'. DERIVED - always "
                "confirmed by the 3-vote LLM check. 'AI' is case-sensitive, so 'IT governance' never matches.",
    },
    {
        "skill": "Program Management",
        "name": "Program Head/Manager/Director (job title)",
        "anchor_terms": ["Program"],          # "Programme" too - spelling is normalised
        "near_terms": ["Head", "Manager", "Director"],
        "max_gap_words": 0,
        "sections": ["EXPERIENCE"],
        "anchor_case_sensitive": False,
        "match_scope": "JOB_TITLE",
        "note": "A held job title such as 'Senior Programme Manager' or 'Program Director'. Parsed work-experience "
                "titles only (not 'target roles'). DERIVED - always confirmed by the 3-vote LLM check.",
    },
]

CHANGE_MANAGEMENT_SENSES = [
    {
        "sense_key": "ITSM",
        "label": "IT service change management (ITSM)",
        "description": "Controlling changes to IT services and systems: change requests, CAB approval, release and "
                       "deployment control, usually alongside incident and problem management (ITIL).",
        "cue_words": ["ITIL", "ITSM", "incident", "incident management", "problem management", "release management",
                      "CAB", "change advisory board", "change request", "change requests", "change control", "RFC",
                      "ServiceNow", "service management", "IT service management", "CMDB", "configuration management",
                      "service desk", "major incident"],
        "is_default": False,
    },
    {
        "sense_key": "ORGANISATIONAL",
        "label": "Organisational change management",
        "description": "Leading the people side of business change: stakeholder engagement, communication, training, "
                       "adoption and resistance management during a transformation.",
        "cue_words": ["organisational", "organizational", "adoption", "stakeholder", "stakeholders", "culture",
                      "cultural", "people", "communications", "Prosci", "ADKAR", "Kotter", "resistance", "readiness",
                      "transformation", "engagement", "behaviour", "behavior", "workforce", "reskilling", "upskilling"],
        "is_default": True,
    },
]


def seed(db) -> None:
    from app.models.config import PlatformConfig
    from app.models.scoring_v2 import SkillScanTerm, SkillSense
    from app.models.skills import SkillOntology

    capabilities = db.query(PlatformConfig).filter(PlatformConfig.key == LLM_MODEL_CAPABILITIES_KEY).first()
    if capabilities is not None:
        rules = json.loads(capabilities.value)
        changed = False
        for rule in rules:
            if rule.get("provider") == "ANTHROPIC" and str(rule.get("source", "")).startswith(_ANTHROPIC_RULE_V1_SOURCE):
                current = next(r for r in DEFAULT_CAPABILITY_RULES if r["provider"] == "ANTHROPIC")
                rule.update(supports_temperature=current["supports_temperature"], source=current["source"])
                changed = True
        if changed:
            capabilities.value = json.dumps(rules, indent=1)
            print(f"Updated config: {LLM_MODEL_CAPABILITIES_KEY} (Anthropic temperature off until verified)")

    if db.query(PlatformConfig).filter(PlatformConfig.key == MATCH_BLOCKLIST_KEY).first() is None:
        db.add(PlatformConfig(
            id=uuid.uuid4(), key=MATCH_BLOCKLIST_KEY, value=json.dumps(MATCH_BLOCKLIST, indent=1),
            description="Scoring v2 only: (written term -> catalogue skill) matches never accepted from any "
                        "source, e.g. a fuzzy match known to be wrong. v1 matching is not affected.",
        ))
        print(f"Added config: {MATCH_BLOCKLIST_KEY}")

    for key, value, description in (
        (SCORING_V2_CONFIG_KEY, json.dumps(SCORING_V2_DEFAULT_CONFIG, indent=1),
         "Scoring v2 weights and multipliers: importance weights (core x2), hierarchy multipliers, evidence "
         "recognition, preferred share, component weights (skills/functional - experience and education are "
         "gates only), near-miss margins for the informational would_be_review flag. Stored with every v2 result."),
        (DAILY_LLM_CALL_CAP_KEY, str(DEFAULT_DAILY_LLM_CALL_CAP),
         "Scoring v2: maximum core-confirmation LLM calls per day (UTC), across all shadow / v2 campaigns. "
         "When reached, v2 is skipped for further candidates that day and shown as 'not run'; v1 is never "
         "affected. Each confirmation needing the LLM costs 3 calls (3 votes), more on invalid-output retries."),
    ):
        if db.query(PlatformConfig).filter(PlatformConfig.key == key).first() is None:
            db.add(PlatformConfig(id=uuid.uuid4(), key=key, value=value, description=description))
            print(f"Added config: {key}")

    # Superseded by the per-campaign hiring_campaigns.scoring_v2_shadow switch;
    # removed only if it still holds the value this seed put there.
    legacy_shadow = db.query(PlatformConfig).filter(PlatformConfig.key == "SCORING_V2_SHADOW").first()
    if legacy_shadow is not None and (legacy_shadow.value or "").strip().lower() == "none":
        db.delete(legacy_shadow)
        print("Removed config: SCORING_V2_SHADOW (replaced by hiring_campaigns.scoring_v2_shadow)")

    if capabilities is None:
        db.add(PlatformConfig(
            id=uuid.uuid4(), key=LLM_MODEL_CAPABILITIES_KEY,
            value=json.dumps(DEFAULT_CAPABILITY_RULES, indent=1),
            description="Per-model LLM request capabilities (temperature support, structured-output method, output "
                        "token limit). First rule matching provider + model_pattern wins; adapters send only what "
                        "the model supports. Each rule's `source` records where its values came from.",
        ))
        print(f"Added config: {LLM_MODEL_CAPABILITIES_KEY}")
    else:
        print(f"Config already exists: {LLM_MODEL_CAPABILITIES_KEY}")

    existing_terms = {row.term_key for row in db.query(SkillScanTerm).all()}
    for term, policy, cues, note in SCAN_TERMS:
        key = term_key(term)
        if key in existing_terms:
            print(f"Scan term already exists: {term}")
            continue
        db.add(SkillScanTerm(id=uuid.uuid4(), term_key=key, policy=policy, cue_words=cues, note=note))
        print(f"Added scan term: {term} -> {policy}")

    from app.models.scoring_v2 import SkillPatternRule
    existing_rules = {row.name for row in db.query(SkillPatternRule).all()}
    for rule in PATTERN_RULES:
        if rule["name"] in existing_rules:
            print(f"Pattern rule already exists: {rule['name']}")
            continue
        skill = db.query(SkillOntology).filter(SkillOntology.canonical_name == rule["skill"]).first()
        if skill is None:
            print(f"Skill {rule['skill']!r} not in the catalogue - pattern rule not seeded.")
            continue
        values = {k: v for k, v in rule.items() if k != "skill"}
        db.add(SkillPatternRule(id=uuid.uuid4(), canonical_skill_id=skill.id, **values))
        print(f"Added pattern rule: {rule['name']}")

    change_management = db.query(SkillOntology).filter(SkillOntology.canonical_name == "Change Management").first()
    if change_management is None:
        print("Skill 'Change Management' not in the catalogue - senses not seeded.")
    else:
        existing = {row.sense_key for row in db.query(SkillSense).filter(
            SkillSense.canonical_skill_id == change_management.id).all()}
        for sense in CHANGE_MANAGEMENT_SENSES:
            if sense["sense_key"] in existing:
                print(f"Sense already exists: Change Management / {sense['sense_key']}")
                continue
            db.add(SkillSense(id=uuid.uuid4(), canonical_skill_id=change_management.id, window_lines=1, **sense))
            print(f"Added sense: Change Management / {sense['sense_key']}")
    db.commit()


if __name__ == "__main__":
    from app.db.session import SessionLocal
    import app.models  # noqa: F401

    session = SessionLocal()
    try:
        seed(session)
        print("\nScoring v2 data seeded successfully")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
