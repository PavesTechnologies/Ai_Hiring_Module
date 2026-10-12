"""Scoring v2 core-skill confirmation: auto-confirm, 3-vote majority, sections, senses, cache (mocked LLM)."""
from types import SimpleNamespace
from uuid import uuid4

from app.services.llm.base import LLMErrorReason, LLMTransientError
from app.services.skills.core_skill_confirmation_service import (
    CONFIRMATION_PROMPT, REASON_AUTO, STATUS_CONFIRMED, STATUS_NOT_FOUND, STATUS_REJECTED, STATUS_SENSE_MISMATCH,
    STATUS_UNCERTAIN, STATUS_UNCONFIRMED, CoreSkillConfirmationService, CoreSkillSpec, jd_core_skill_specs,
)
from app.services.skills.resume_sections import heading_section, section_of_line
from app.services.skills.skill_sense_resolver import SenseDef, SkillSenseResolver

PM, CM, AT, AG = uuid4(), uuid4(), uuid4(), uuid4()
RESUME, JD = uuid4(), uuid4()


class FakeProvider:
    provider_name = "GOOGLE"
    model = "main-model"

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []
        self.last_model_used = None
        self.last_used_fallback = False

    def generate_json(self, prompt, response_schema, *, deterministic=False):
        self.calls.append({"prompt": prompt, "deterministic": deterministic})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):  # (payload, answering model, used_fallback)
            reply, self.last_model_used, self.last_used_fallback = reply
        return reply


class FakeRepo:
    def __init__(self):
        self.rows = {}

    def get_confirmation(self, key):
        return self.rows.get(key)

    def put_confirmation(self, row):
        self.rows[row.cache_key] = row


def _row(skill_id, text, *, source="SCAN", section="SKILLS", sense=None, sources=None):
    return {"canonical_skill_id": skill_id, "sense": sense, "sources": list(sources or [source]),
            "evidence": [{"text": text, "source": source, "origin": "x", "section": section}]}


def _yes(n, quote):
    return {"number": n, "present": True, "quote": quote}


def _no(n):
    return {"number": n, "present": False, "quote": ""}


def _confirm(provider, specs, evidence, repo=None, **kw):
    return CoreSkillConfirmationService(provider, repo).confirm(
        resume_id=RESUME, jd_id=JD, core_skills=specs, evidence_rows=evidence, text_hash="h1", **kw)


def _by_name(outcome):
    return {r["name"]: r for r in outcome.results}


ORG_CM = CoreSkillSpec(CM, "Change Management", sense="ORGANISATIONAL", sense_label="Organisational change management",
                       sense_description="People side", sense_basis="cue")


# --- auto-confirm (no LLM) -----------------------------------------------

def test_exact_scan_in_strong_section_is_auto_confirmed_without_llm():
    for section in ("SKILLS", "EXPERIENCE", "SUMMARY"):
        provider = FakeProvider([])
        outcome = _confirm(provider, [CoreSkillSpec(PM, "Program Management")],
                           [_row(PM, "IT Project & Program Management", section=section)])
        pm = outcome.results[0]
        assert provider.calls == [] and (pm["status"], pm["reason"], pm["method"]) == (STATUS_CONFIRMED, REASON_AUTO, "auto")


def test_not_auto_confirmed_when_derived_inferred_weak_section_or_unclear_sense():
    reply = {"items": [_no(1)]}
    cases = [
        ([CoreSkillSpec(AT, "AI Transformation")], [_row(AT, "AI & Digital Transformation", source="DERIVED")]),
        ([CoreSkillSpec(PM, "Program Management")], [_row(PM, "PMP certified lead", source="INFERRED")]),
        ([CoreSkillSpec(AG, "AI Governance")], [_row(AG, "Local chapter - AI Governance", section="RECOGNITION")]),
        ([CoreSkillSpec(AG, "AI Governance")], [_row(AG, "AI Governance meetup", section="EVENTS")]),
        ([ORG_CM], [_row(CM, "Change Management", sense=None)]),                         # unresolved sense
        ([CoreSkillSpec(CM, "Change Management", sense="ORGANISATIONAL", sense_basis="assumed")],
         [_row(CM, "Change Management", sense="ORGANISATIONAL")]),                       # JD sense only assumed
    ]
    for specs, evidence in cases:
        provider = FakeProvider([reply] * 3)
        outcome = _confirm(provider, specs, evidence)
        assert len(provider.calls) == 3, evidence
        assert outcome.results[0]["status"] == STATUS_REJECTED


def test_weak_section_excerpt_is_labelled_for_the_llm():
    provider = FakeProvider([{"items": [_no(1)]}] * 3)
    _confirm(provider, [CoreSkillSpec(AG, "AI Governance")], [_row(AG, "Local chapter - AI Governance", section="RECOGNITION")])
    assert '[Recognition] "Local chapter - AI Governance"' in provider.calls[0]["prompt"]


def test_prompt_asks_the_own_skill_question():
    assert ("Does the resume present this as the candidate's own skill or experience, rather than an event, "
            "award title, organisation name or something they only mention?") in CONFIRMATION_PROMPT


# --- 3-vote majority ------------------------------------------------------

def _asked(replies, repo=None, **kw):
    provider = FakeProvider(replies)
    outcome = _confirm(provider, [CoreSkillSpec(AT, "AI Transformation")],
                       [_row(AT, "AI & Digital Transformation", source="DERIVED")], repo, **kw)
    return outcome, provider


def test_three_yes_confirms_three_no_rejects_mixed_is_uncertain():
    yes = {"items": [_yes(1, "AI & Digital Transformation")]}
    no = {"items": [_no(1)]}
    confirmed, provider = _asked([yes, yes, yes])
    assert confirmed.results[0]["status"] == STATUS_CONFIRMED and len(provider.calls) == 3
    assert all(c["deterministic"] for c in provider.calls)
    assert _asked([no, no, no])[0].results[0]["status"] == STATUS_REJECTED
    mixed = _asked([yes, no, yes])[0].results[0]
    assert (mixed["status"], mixed["votes"]) == (STATUS_UNCERTAIN, ["yes", "no", "yes"])


def test_yes_with_a_quote_not_in_the_excerpts_is_not_a_yes():
    bad = {"items": [_yes(1, "something the resume never says")]}
    yes = {"items": [_yes(1, "AI & Digital Transformation")]}
    # vote 1: invalid twice (retried once) -> invalid; votes 2 and 3 yes -> mixed
    outcome, provider = _asked([bad, bad, yes, yes])
    assert outcome.results[0]["status"] == STATUS_UNCERTAIN and len(provider.calls) == 4


def test_every_call_failing_leaves_it_unconfirmed():
    down = LLMTransientError("down", reason=LLMErrorReason.UNAVAILABLE)
    outcome, _ = _asked([down, down, down])
    assert outcome.results[0]["status"] == STATUS_UNCONFIRMED and outcome.problem


# --- senses ----------------------------------------------------------------

def test_definite_sense_mismatch_skips_the_llm():
    provider = FakeProvider([])
    outcome = _confirm(provider, [ORG_CM], [_row(CM, "Change, Incident & Problem Management", sense="ITSM")])
    assert provider.calls == [] and outcome.results[0]["status"] == STATUS_SENSE_MISMATCH


def test_auto_confirm_needs_the_definite_matching_sense():
    provider = FakeProvider([])
    outcome = _confirm(provider, [ORG_CM], [_row(CM, "Led change management", sense="ORGANISATIONAL", section="EXPERIENCE")])
    assert provider.calls == [] and outcome.results[0]["status"] == STATUS_CONFIRMED


def test_not_found_needs_no_llm():
    provider = FakeProvider([])
    assert _confirm(provider, [CoreSkillSpec(AT, "AI Transformation")], []).results[0]["status"] == STATUS_NOT_FOUND


def test_alias_evidence_counts_for_the_core_skill():
    alias = uuid4()
    provider = FakeProvider([])
    outcome = _confirm(provider, [CoreSkillSpec(PM, "ServiceNow SPM", alias_skill_ids=(alias,))],
                       [_row(alias, "ITBM roadmap delivery", section="EXPERIENCE")])
    assert outcome.results[0]["status"] == STATUS_CONFIRMED


# --- cache -----------------------------------------------------------------

def test_only_confirmed_and_rejected_are_cached_uncertain_is_asked_again():
    repo = FakeRepo()
    yes = {"items": [_yes(1, "AI & Digital Transformation")]}
    no = {"items": [_no(1)]}
    _asked([yes, no, yes], repo)
    assert repo.rows == {}                                     # uncertain: nothing cached
    _asked([no, no, no], repo)
    assert len(repo.rows) == 1                                 # rejected: cached
    again, provider = _asked([], repo)
    assert again.cached and provider.calls == [] and again.results[0]["status"] == STATUS_REJECTED


def test_no_votes_from_the_backup_model_are_uncertain_and_not_cached():
    repo = FakeRepo()
    no = ({"items": [_no(1)]}, "backup-model", True)
    outcome, _ = _asked([no, no, no], repo)
    assert outcome.results[0]["status"] == STATUS_UNCERTAIN and repo.rows == {}
    assert outcome.results[0]["reason"] == "no_votes_include_backup_model"


def test_one_backup_vote_among_no_votes_still_means_uncertain():
    no, backup_no = {"items": [_no(1)]}, ({"items": [_no(1)]}, "backup-model", True)
    outcome, _ = _asked([no, backup_no, no])
    assert outcome.results[0]["status"] == STATUS_UNCERTAIN


def test_yes_votes_from_the_backup_model_still_confirm():
    yes = ({"items": [_yes(1, "AI & Digital Transformation")]}, "backup-model", True)
    outcome, _ = _asked([yes, yes, yes])
    assert outcome.results[0]["status"] == STATUS_CONFIRMED
    assert outcome.results[0]["llm"]["model"] == "backup-model" and outcome.results[0]["llm"]["used_fallback"]


# --- JD specs, sections ---------------------------------------------------

def test_jd_core_specs_resolve_sense_and_skip_supporting():
    senses = [
        SenseDef(CM, "ITSM", "ITSM", "d", ("ITIL", "CAB")),
        SenseDef(CM, "ORGANISATIONAL", "Org", "d", ("adoption",), is_default=True),
    ]
    jd = SimpleNamespace(title="AI Transformation Lead", raw_text="Drive change management.", extracted_json={})
    rows = [
        SimpleNamespace(canonical_skill_id=CM, mandatory=True, importance=SimpleNamespace(value="CORE"), alias_skill_ids=None),
        SimpleNamespace(canonical_skill_id=PM, mandatory=True, importance=SimpleNamespace(value="SUPPORTING"), alias_skill_ids=None),
    ]
    specs = jd_core_skill_specs(jd, rows, {CM: "Change Management", PM: "Program Management"}, SkillSenseResolver(senses))
    assert [(s.name, s.sense, s.sense_basis) for s in specs] == [("Change Management", "ORGANISATIONAL", "assumed")]


def test_section_detection():
    lines = ["JANE D", "Senior leader in program management", "R E C O G N I T I O N", "Local chapter - AI Governance",
             "Core Competencies", "Program Management", "RHEL 9", "Work Experience", "Led change"]
    assert heading_section("R E C O G N I T I O N") == "RECOGNITION"
    assert heading_section("RHEL 9") is None and heading_section("JANE D") is None
    assert [section_of_line(lines, i) for i in (1, 3, 5, 6, 8)] == ["SUMMARY", "RECOGNITION", "SKILLS", "SKILLS", "EXPERIENCE"]
