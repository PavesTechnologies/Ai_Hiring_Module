"""
Phase 3 D4 (tests/fixtures/phase3/D4.docx): an organisational change
management consultant (Prosci, ADKAR, stakeholder communication, ERP
rollout) against a JD whose core Change Management is the ITSM sense,
confirmed by HR.

Seen live: the bare job-title line "Change Management Lead - Harbourstone
Consulting" had no cue words near it, so it stayed unresolved, was taken as
compatible with ITSM, credited EXACT and confirmed by the LLM. It must take
the resume's own (organisational) sense: a sense mismatch, no LLM call, no
core credit.
"""
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from app.models.resume.resume_source_format import ResumeSourceFormat
from app.seeds.seed_scoring_v2 import CHANGE_MANAGEMENT_SENSES
from app.services.campaign.scoring_v2 import EvidenceInput, JDSkillInput, ScoringInputs, merge_config, score_v2
from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
from app.services.skills.core_skill_confirmation_service import (
    STATUS_SENSE_MISMATCH, CoreSkillConfirmationService, CoreSkillSpec,
)
from app.services.skills.resume_skill_scanner import ScanIndex
from app.services.skills.skill_evidence_service import build_evidence
from app.services.skills.skill_sense_resolver import SenseDef, SkillSenseResolver

D4 = Path(__file__).resolve().parents[2] / "fixtures" / "phase3" / "D4.docx"
CM = uuid4()
SENSES = [SenseDef(CM, s["sense_key"], s["label"], s["description"], tuple(s["cue_words"]), s["is_default"])
          for s in CHANGE_MANAGEMENT_SENSES]
ITSM_CORE = CoreSkillSpec(CM, "Change Management", sense="ITSM", sense_label="IT service change management (ITSM)",
                          sense_description="ITIL change control", sense_basis="hr")


class NoLLM:
    provider_name, model = "GOOGLE", "none"
    last_model_used, last_used_fallback = None, False

    def generate_json(self, *args, **kwargs):
        raise AssertionError("the LLM must not be asked about a definite sense mismatch")


def _d4_evidence():
    text = ResumeTextExtractionService.extract(D4.read_bytes(), ResumeSourceFormat.DOCX)
    index = ScanIndex.build([SimpleNamespace(id=CM, canonical_name="Change Management", aliases=[])], {})
    records = build_evidence(text=text, index=index, resolver=SkillSenseResolver(SENSES), llm_matches=[])
    return text, [r for r in records if r.canonical_skill_id == CM]


def test_every_change_management_mention_in_d4_is_organisational():
    text, records = _d4_evidence()
    assert "Change Management Lead" in text            # the uncued job-title line is in the fixture
    assert [(r.sense, r.sense_resolved) for r in records] == [("ORGANISATIONAL", True)]
    assert any("Change Management Lead" in e["text"] for e in records[0].evidence)


def test_d4_is_a_sense_mismatch_for_an_itsm_jd_without_asking_the_llm():
    _, records = _d4_evidence()
    rows = [{"canonical_skill_id": r.canonical_skill_id, "sense": r.sense, "sources": r.sources,
             "evidence": r.evidence} for r in records]
    outcome = CoreSkillConfirmationService(NoLLM(), None).confirm(
        resume_id=uuid4(), jd_id=uuid4(), core_skills=[ITSM_CORE], evidence_rows=rows, text_hash="d4")
    assert outcome.results[0]["status"] == STATUS_SENSE_MISMATCH

    evidence = [EvidenceInput(r.canonical_skill_id, r.sense, tuple(r.sources), r.best_source, r.match_tier,
                              float(r.recognition), r.evidence[0]["text"]) for r in records]
    jd = [JDSkillInput(CM, "Change Management", True, 1.0, "core", sense="ITSM", sense_basis="hr")]
    result = score_v2(ScoringInputs(jd_skills=jd, evidence=evidence,
                                    confirmations={str(CM): {"status": STATUS_SENSE_MISMATCH}},
                                    hierarchy={"skills_by_id": {}, "children_by_parent": {}, "grandchildren_by_parent": {}},
                                    deterministic_threshold=40.0, allowed_missing_core=0), merge_config(None))
    entry = result["mandatory_skills"][0]
    assert (entry["match_type"], entry["counts_for_core_gate"]) == ("SENSE_MISMATCH", False)
    assert result["core_sense_mismatch"] == ["Change Management"]
