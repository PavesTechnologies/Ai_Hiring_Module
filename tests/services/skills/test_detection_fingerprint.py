"""The detection version carries a fingerprint of the detection rule data: any rule change makes stored evidence stale."""
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.skills.certification_mapping_service import rule_from
from app.services.skills.detection_fingerprint import detection_fingerprint, detection_version
from app.services.skills.evidence_rules import DEFAULT_RULES, EvidenceRules
from app.services.skills.resume_skill_scanner import DETECTION_VERSION, ScanTermRule
from app.services.skills.skill_sense_resolver import SenseDef

ML, CM, GIT = uuid4(), uuid4(), uuid4()


def _pattern(**overrides):
    from app.services.skills.resume_skill_scanner import PatternRule
    values = dict(skill_id=GIT, name="GitHub implies Git", anchor_terms=("GitHub", "GitLab"), near_terms=(),
                  max_gap_words=0, sections=frozenset({"EXPERIENCE", "PROJECTS"}), anchor_case_sensitive=False,
                  match_scope="LINE")
    values.update(overrides)
    return PatternRule(**values)


def _data(**overrides):
    data = dict(
        catalog=[SimpleNamespace(id=ML, canonical_name="Machine Learning", aliases=["ML", "machine-learning"]),
                 SimpleNamespace(id=CM, canonical_name="Change Management", aliases=[])],
        scan_rules={"ml": ScanTermRule("REQUIRE_CUE", ("model", "platform"))},
        senses=[SenseDef(CM, "ITSM", "IT service change control", "CAB, release", ("cab", "itil"))],
        blocked=frozenset({("it governance", ML)}),
        patterns=(_pattern(),),
        certifications=(rule_from("ITIL 4 Foundation", ["ITIL v4"], [(CM, "ITSM")]),),
        rules=DEFAULT_RULES,
    )
    data.update(overrides)
    return data


def test_the_same_rule_data_in_any_order_gives_the_same_fingerprint():
    a = detection_fingerprint(**_data())
    shuffled = _data(catalog=list(reversed(_data()["catalog"])),
                     patterns=(_pattern(sections=frozenset({"PROJECTS", "EXPERIENCE"})),))
    assert detection_fingerprint(**shuffled) == a


@pytest.mark.parametrize("change", [
    {"catalog": [SimpleNamespace(id=ML, canonical_name="Machine Learning", aliases=["ML", "machine-learning", "AI/ML"]),
                 SimpleNamespace(id=CM, canonical_name="Change Management", aliases=[])]},          # an alias
    {"scan_rules": {"ml": ScanTermRule("REQUIRE_CUE", ("model", "platform", "engineer"))}},       # a cue word
    {"scan_rules": {}},                                                                            # a scan term
    {"senses": [SenseDef(CM, "ITSM", "IT service change control", "CAB, release", ("cab", "itil", "rfc"))]},
    {"blocked": frozenset()},                                                                      # the blocklist
    {"patterns": (_pattern(anchor_terms=("GitHub", "GitLab", "Bitbucket")),)},                    # a pattern rule
    {"certifications": (rule_from("ITIL 4 Foundation", ["ITIL v4"], [(CM, "ITSM"), (ML, None)]),)},  # a mapping
    {"rules": EvidenceRules.from_config({"work_verbs": ["built", "shipped"]})},                   # the verb lists
])
def test_any_detection_rule_change_gives_a_new_fingerprint(change):
    assert detection_fingerprint(**_data(**change)) != detection_fingerprint(**_data())


def test_a_sense_label_or_description_is_not_a_detection_rule():
    relabelled = [SenseDef(CM, "ITSM", "IT change control", "Change advisory board, releases", ("cab", "itil"))]
    assert detection_fingerprint(**_data(senses=relabelled)) == detection_fingerprint(**_data())


def test_the_version_is_the_code_version_plus_the_fingerprint_and_fits_the_column():
    version = detection_version(detection_fingerprint(**_data()))
    assert version.startswith(DETECTION_VERSION + "+") and len(version) <= 20     # detection_version String(20)


def test_detection_stamps_evidence_with_the_fingerprinted_version():
    from unittest.mock import MagicMock, patch
    from app.services.skills.skill_evidence_service import ParseInfo, SkillEvidenceService
    repo = MagicMock()
    repo.load_scan_rules.return_value = _data()["scan_rules"]
    repo.load_senses.return_value = _data()["senses"]
    repo.load_match_blocklist.return_value = []
    repo.load_pattern_rules.return_value = list(_data()["patterns"])
    repo.load_certification_rules.return_value = list(_data()["certifications"])
    repo.load_evidence_rules_config.return_value = None
    service = SkillEvidenceService(repo, MagicMock())
    with patch("app.services.skills.skill_normalization_service.load_cached_active_skills",
               return_value=_data()["catalog"]):
        tools = service.load_tools()
        service.scan_provisional_skills = MagicMock()
        service.detect(uuid4(), text="Built an ML platform with MLflow", llm_matches=[],
                       parse=ParseInfo(provider=None, model=None, used_fallback=False))
    assert tools.version.startswith(DETECTION_VERSION + "+") and tools.version != DETECTION_VERSION
    rows = repo.replace_evidence.call_args.args[1]
    assert rows and all(r.detection_version == tools.version for r in rows)
