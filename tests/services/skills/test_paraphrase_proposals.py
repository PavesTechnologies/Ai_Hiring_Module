"""
Phase 3 Step 6: paraphrase proposals for core skills still NOT_FOUND /
UNCERTAIN. Similar experience lines (top 3 above the threshold) are proposed
to the 3-vote confirmation; never lines already quoted, lines with a
blocklisted term for that skill, or lines whose own cues put them in another
sense than the JD's. The embedding here is a fixed keyword model so the
selection rules are tested exactly.
"""
import math
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest

from app.services.skills.evidence_rules import DEFAULT_RULES

from app.services.skills.paraphrase_proposal_service import (
    ParaphraseProposalService, ParaphraseTarget, candidate_lines, proposals_hash, skill_text,
)
from app.services.skills.resume_skill_scanner import term_key
from app.services.skills.skill_sense_resolver import SenseDef, SkillSenseResolver

INC, CM, CAT, AIG = (uuid4() for _ in range(4))
_TOPICS = [("ticket", "outage", "incident"), ("rfc", "release", "approval", "itsm"),
           ("request", "portal", "catalog", "catalogue", "fulfilment"), ("governance", "policy"),
           ("stakeholder", "adoption", "erp", "people")]


def fake_embed(texts):
    out = []
    for text in texts:
        low = text.lower()
        vec = [float(sum(low.count(w) for w in words)) for words in _TOPICS] + [0.2]
        norm = math.sqrt(sum(v * v for v in vec))
        out.append([v / norm for v in vec])
    return out


SENSES = [SenseDef(CM, "ITSM", "IT service change", "", ("rfc", "cab", "itil", "release management")),
          SenseDef(CM, "ORGANISATIONAL", "People side", "", ("stakeholder", "adoption", "erp"), is_default=True)]
RESOLVER = SkillSenseResolver(SENSES)
TARGETS = [
    ParaphraseTarget(INC, "Incident Management", description="Handling IT incidents and outages, tickets"),
    ParaphraseTarget(CM, "Change Management", sense="ITSM", sense_basis="hr",
                     sense_description="RFC approval and release control (ITSM)"),
    ParaphraseTarget(CAT, "Service Catalog", description="ServiceNow catalogue items, request portal, fulfilment"),
]
D2 = "\n".join([
    "Summary", "ServiceNow platform specialist with 5 years supporting IT operations teams.",
    "Experience",
    "Owned ticket handling and outage response workflows on ServiceNow for 4,000 users",
    "Configured the RFC process, release approvals and the weekly approval board on ServiceNow",
    "Built request portal items and fulfilment flows for laptops, access and software",
    "Skills", "ServiceNow, Flow Designer, REST integrations, reporting",
])
CONFIG = {"threshold": 0.6, "max_lines": 3}


def _propose(text, targets=TARGETS, rows=(), blocked=frozenset(), config=CONFIG):
    return ParaphraseProposalService(fake_embed, RESOLVER, blocked).propose(text, list(targets), list(rows), config)


def _by_skill(proposals):
    out = {}
    for p in proposals:
        out.setdefault(p.canonical_skill_id, []).append(p.line)
    return out


def test_phase3_d2_paraphrases_are_proposed_for_the_right_skills():
    proposals, scores = _propose(D2)
    found = _by_skill(proposals)
    assert found[INC] == ["Owned ticket handling and outage response workflows on ServiceNow for 4,000 users"]
    assert found[CM] == ["Configured the RFC process, release approvals and the weekly approval board on ServiceNow"]
    assert found[CAT] == ["Built request portal items and fulfilment flows for laptops, access and software"]
    assert all(p.section == "EXPERIENCE" and p.similarity >= 0.6 for p in proposals)
    assert len(scores[str(INC)]) == 3                     # every experience line is scored, for the dry run


def test_only_experience_lines_of_a_usable_length_are_searched():
    lines = [line for _, line, _ in candidate_lines(D2, {"sections": ["EXPERIENCE"], "min_words": 4,
                                                         "max_words": 40})]
    assert len(lines) == 3 and all("ServiceNow, Flow Designer" not in line for line in lines)
    assert not any(line.startswith("ServiceNow platform specialist") for line in lines)   # summary


def test_at_most_max_lines_per_skill_best_first():
    text = "Experience\n" + "\n".join(f"Resolved {n} incident tickets and outage escalations" for n in range(5))
    proposals, _ = _propose(text, targets=TARGETS[:1], config={"threshold": 0.1, "max_lines": 3})
    assert len(proposals) == 3


def test_nothing_below_the_threshold_is_proposed():
    proposals, _ = _propose(D2, config={"threshold": 1.01, "max_lines": 3})
    assert proposals == []


def test_phase3_d4_organisational_change_lines_are_never_proposed_for_itsm_change():
    d4 = "\n".join(["Experience", "Led change management for an ERP rollout affecting 6,000 employees",
                    "Ran stakeholder analysis and adoption plans for the release of new processes"])
    proposals, scores = _propose(d4, targets=[TARGETS[1]], config={"threshold": 0.0, "max_lines": 3})
    assert proposals == []
    assert {s["excluded"] for s in scores[str(CM)]} == {"sense ORGANISATIONAL"}


def test_a_line_already_quoted_as_evidence_is_not_proposed_again():
    row = {"canonical_skill_id": INC, "evidence": [
        {"text": "Owned ticket handling and outage response workflows on ServiceNow for 4,000 users"}]}
    proposals, _ = _propose(D2, targets=TARGETS[:1], rows=[row])
    assert proposals == []


def test_a_blocklisted_term_keeps_the_line_out_for_that_skill():
    text = "Experience\nLed IT governance and policy reviews for the PMO portfolio"
    target = ParaphraseTarget(AIG, "AI Governance", description="AI policy and governance")
    blocked = frozenset({(term_key("IT governance"), AIG)})
    assert _propose(text, targets=[target], blocked=blocked, config={"threshold": 0.0})[0] == []
    assert len(_propose(text, targets=[target], config={"threshold": 0.0})[0]) == 1      # not blocked: proposed


def test_the_skill_text_uses_name_aliases_description_and_the_jd_sense():
    target = ParaphraseTarget(CM, "Change Management", aliases=("Change Control",), description="Managing change",
                              sense="ITSM", sense_description="RFC approval and release control")
    assert skill_text(target) == ("Change Management. Also: Change Control. Managing change. "
                                  "RFC approval and release control.")


def test_the_proposals_hash_depends_on_the_lines():
    proposals, _ = _propose(D2)
    assert proposals_hash(proposals) == proposals_hash(list(reversed(proposals)))
    assert proposals_hash(proposals) != proposals_hash(proposals[:1])


def test_a_line_already_evidence_for_another_core_skill_is_not_proposed():
    # Phase 3 RUBELA: "...led global AI program governance" already counts for AI Governance.
    gov = uuid4()
    line = "Led outage response and incident ticket triage for the service desk"
    text = "\n".join(["Experience", line])
    row = {"canonical_skill_id": gov, "evidence": [{"text": line}]}
    # gov isn't a core skill here: its evidence doesn't keep the line out.
    proposals, _ = _propose(text, targets=TARGETS[:1], rows=[row])
    assert [p.line for p in proposals] == [line]
    # gov is a core skill: the line is already its evidence, so it isn't proposed for Incident Management.
    proposals, scores = ParaphraseProposalService(fake_embed, RESOLVER).propose(
        text, TARGETS[:1], [row], CONFIG, core_skill_ids=[INC, gov])
    assert proposals == [] and scores[str(INC)][0]["excluded"] == "evidence for another core skill"


def test_a_line_can_be_proposed_for_several_searched_skills_each_vote_decides():
    # Phase 3 D2: "ticket handling ... on ServiceNow" is incident work AND ServiceNow work.
    line = "Handled incident tickets and outage escalations through the request portal"
    text = "\n".join(["Experience", line])
    proposals, scores = _propose(text, targets=[TARGETS[0], TARGETS[2]], config={"threshold": 0.1, "max_lines": 3})
    assert sorted(str(p.canonical_skill_id) for p in proposals) == sorted([str(INC), str(CAT)])
    assert all(s["excluded"] is None for s in scores[str(CAT)])


# --- the round in scoring v2 ------------------------------------------------------------

def _spec(skill_id, name):
    return SimpleNamespace(canonical_skill_id=skill_id, name=name, sense=None, sense_basis=None,
                           sense_description=None, alias_skill_ids=())


def test_only_not_found_or_uncertain_core_skills_are_searched():
    from app.services.campaign.scoring_v2_service import _paraphrase_targets_for
    ids = [uuid4() for _ in range(6)]
    statuses = ["NOT_FOUND", "UNCERTAIN", "CONFIRMED", "REJECTED", "SENSE_MISMATCH", None]
    specs = [_spec(i, f"S{n}") for n, i in enumerate(ids)]
    confirmations = {str(i): {"status": s} for i, s in zip(ids, statuses) if s}
    targets = _paraphrase_targets_for(specs, confirmations, {})
    assert [t.name for t in targets] == ["S0", "S1", "S5"]          # no result at all = not found


def test_the_round_asks_again_with_the_proposals_under_its_own_cache_key():
    from app.services.campaign import scoring_v2_service as svc
    service = svc.ScoringV2Service.__new__(svc.ScoringV2Service)
    service.db, service.provider_resolver, service.detection_repository = MagicMock(), MagicMock(), MagicMock()
    service._budget = MagicMock(return_value=True)
    service.db.query.return_value.filter.return_value.all.return_value = []
    specs = [_spec(INC, "Incident Management"), _spec(CAT, "Service Catalog")]
    confirmations = {str(INC): {"status": "NOT_FOUND"}, str(CAT): {"status": "NOT_FOUND"}}
    outcome = SimpleNamespace(
        skipped_budget=False, cached=False, llm_calls=3, problem=None, llm_unavailable=False,
        results=[{"canonical_skill_id": str(INC), "status": "CONFIRMED"},
                 {"canonical_skill_id": str(CAT), "status": "REJECTED"}])
    tools = SimpleNamespace(resolver=RESOLVER, blocked=frozenset(), rules=DEFAULT_RULES)
    targets = [ParaphraseTarget(INC, "Incident Management", description="incidents tickets outage"),
               ParaphraseTarget(CAT, "Service Catalog", description="catalogue request portal fulfilment")]
    with patch.object(svc, "redacted_resume_text", return_value=D2), \
            patch.object(svc, "_paraphrase_targets_for", return_value=targets), \
            patch("app.services.ai.embedding_service.EmbeddingService") as embedding, \
            patch.object(svc, "CoreSkillConfirmationService") as confirm_cls:
        embedding.return_value.generate_embeddings.side_effect = fake_embed
        confirm_cls.return_value.confirm.return_value = outcome
        roles = {"work_experience": [{"title": "ServiceNow Administrator", "company": "Northwind IT",
                                      "description": "; ".join(D2.splitlines()[3:6])}]}
        info, extra = service._paraphrase_round(
            resume=SimpleNamespace(id=uuid4(), parsed_json=roles),
            job_description=SimpleNamespace(id=uuid4(), title="ServiceNow ITSM Consultant", role_domain="IT_OPERATIONS"),
            specs=specs, confirmations=confirmations, evidence_rows=[], tools=tools, config=CONFIG, text_hash="h",
            persist=True, use_cache=True)
    kwargs = confirm_cls.return_value.confirm.call_args.kwargs
    # Like any experience excerpt, a proposed line carries its role; the vote sees the job and its domain.
    assert {item.get("role") for r in kwargs["evidence_rows"] for item in r["evidence"]} == {
        "ServiceNow Administrator, Northwind IT"}
    assert (kwargs["job"].title, kwargs["job"].role_domain) == ("ServiceNow ITSM Consultant", "IT operations")
    # Fits core_skill_confirmations.text_hash (String(64)); differs from the first round's.
    assert len(kwargs["text_hash"]) == 64 and int(kwargs["text_hash"], 16) >= 0 and kwargs["text_hash"] != "h"
    assert {s.name for s in kwargs["core_skills"]} == {"Incident Management", "Service Catalog"}
    assert all(r["sources"] == ["DERIVED"] for r in kwargs["evidence_rows"])
    assert confirmations[str(INC)]["status"] == "CONFIRMED" and confirmations[str(INC)]["paraphrase"] is True
    assert confirmations[str(CAT)]["status"] == "REJECTED"           # all no: the proposal is discarded
    assert [e.canonical_skill_id for e in extra] == [INC] and extra[0].recognition == 0.8
    assert info["llm_calls"] == 3 and len(info["proposals"]) == 2


def test_no_proposal_means_no_llm_call():
    from app.services.campaign import scoring_v2_service as svc
    service = svc.ScoringV2Service.__new__(svc.ScoringV2Service)
    service.db, service.provider_resolver, service.detection_repository = MagicMock(), MagicMock(), MagicMock()
    service.db.query.return_value.filter.return_value.all.return_value = []
    targets = [ParaphraseTarget(INC, "Incident Management", description="incidents tickets outage")]
    with patch.object(svc, "redacted_resume_text", return_value="Experience\nDesigned Java microservices on Kubernetes"), \
            patch.object(svc, "_paraphrase_targets_for", return_value=targets), \
            patch("app.services.ai.embedding_service.EmbeddingService") as embedding, \
            patch.object(svc, "CoreSkillConfirmationService") as confirm_cls:
        embedding.return_value.generate_embeddings.side_effect = fake_embed
        info, extra = service._paraphrase_round(
            resume=SimpleNamespace(id=uuid4()), job_description=SimpleNamespace(id=uuid4()),
            specs=[_spec(INC, "Incident Management")], confirmations={}, evidence_rows=[],
            tools=SimpleNamespace(resolver=RESOLVER, blocked=frozenset(), rules=DEFAULT_RULES), config=CONFIG, text_hash="h",
            persist=True, use_cache=True)
    confirm_cls.return_value.confirm.assert_not_called()
    assert info["llm_calls"] == 0 and extra == []


# --- job-title lines and role headers are never proposed; bullets mentioning a title word are ------

from app.services.skills.paraphrase_proposal_service import is_title_line  # noqa: E402

TITLES = ["Data Engineer", "Head of Data Engineering", "Software Engineer", "Director, Data & ML Platforms"]


@pytest.mark.parametrize("line", [
    "Data Engineer — Northstar Analytics · Oct 2011 – Sep 2015",                 # senior S1
    "Software Engineer — Spice Coast Software · Oct 2006 – Sep 2012",            # senior S7
    "Head of Data Engineering",                                                  # the title alone
    "Director, Data & ML Platforms — Lumen Commerce · Oct 2019 – present",
    "Head of Data Engineering, Orbit Payments",
    "Senior Data Scientist · Brightwater Advisory · Jan 2020 – Mar 2022",        # a role header, title not parsed
])
def test_title_lines_and_role_headers_are_skipped(line):
    assert is_title_line(line, TITLES)


@pytest.mark.parametrize("line", [
    "Led the Data Engineering team that built the lakehouse and streaming pipelines",   # mentions a title word
    "Data Engineer onboarding: built the ingestion framework new hires start from",     # title then no separator
    "Built batch and streaming pipelines in Spark (2019 – 2021)",                      # dates, no header mark
    "Migrated the warehouse to Databricks — Oct 2020 – Mar 2021",                      # a work verb: a bullet
])
def test_bullets_that_mention_a_title_word_stay_evidence(line):
    assert not is_title_line(line, TITLES)


def test_candidate_lines_drop_title_lines_but_keep_the_bullets():
    text = ("Experience\n"
            "Data Engineer — Northstar Analytics · Oct 2011 – Sep 2015\n"
            "- Led the Data Engineering team that built the lakehouse and streaming pipelines\n"
            "- Wrote ETL pipelines in Python and SQL for retail reporting\n")
    lines = [line for _, line, _ in candidate_lines(text, {"sections": ["EXPERIENCE"], "min_words": 4,
                                                            "max_words": 40}, job_titles=TITLES)]
    assert lines == ["Led the Data Engineering team that built the lakehouse and streaming pipelines",
                     "Wrote ETL pipelines in Python and SQL for retail reporting"]
