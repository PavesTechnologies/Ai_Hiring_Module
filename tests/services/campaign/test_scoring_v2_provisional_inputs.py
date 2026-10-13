"""
v2 provisional inputs: detection scans the redacted resume text for every
unresolved JD unknown skill and stores what it scanned and found; scoring
reads that, and scans the stored resume again when a JD unknown skill is
newer than the scan.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

from app.services.campaign.scoring_v2_service import ScoringV2Service
from app.services.skills.provisional_skill_match import ProvisionalTerm
from app.services.skills.skill_evidence_service import SkillEvidenceService

V2 = "app.services.campaign.scoring_v2_service"


def _pending(raw_text, mandatory=True):
    return (SimpleNamespace(mandatory=mandatory), SimpleNamespace(id=uuid4(), raw_text=raw_text, normalized_key=None))


def _service(pending, scan):
    repo = MagicMock()
    repo.pending_for_jd.return_value = pending
    repo.get_scan.return_value = scan
    service = ScoringV2Service(MagicMock(), MagicMock(), MagicMock())
    service.evidence_service = MagicMock()
    return service, repo


JD = SimpleNamespace(id=uuid4(), extracted_json={"required_skills": {"core": ["Agentic Workflows"]}})
RESUME = SimpleNamespace(id=uuid4())


def test_inputs_come_from_the_stored_scan_when_it_covers_every_pending_skill():
    agentic, prompt = _pending("Agentic Workflows"), _pending("Prompt Design", mandatory=False)
    scan = SimpleNamespace(scanned_unknown_skill_ids=[agentic[1].id, prompt[1].id],
                           hits=[{"unknown_skill_id": str(agentic[1].id), "excerpt": "Built agentic workflows"}])
    service, repo = _service([agentic, prompt], scan)
    with patch(f"{V2}.ProvisionalSkillRepository", return_value=repo), patch(f"{V2}.redacted_resume_text") as text:
        inputs = service.provisional_inputs(JD, RESUME)

    text.assert_not_called()
    assert [(i.name, i.tier, i.found, i.scanned) for i in inputs] == [
        ("Agentic Workflows", "CORE", True, True), ("Prompt Design", "PREFERRED", False, True)]
    assert inputs[0].excerpt == "Built agentic workflows"


def test_a_pending_skill_newer_than_the_scan_triggers_a_rescan_of_the_stored_resume():
    agentic = _pending("Agentic Workflows")
    scan = SimpleNamespace(scanned_unknown_skill_ids=[], hits=[])
    service, repo = _service([agentic], scan)
    service.evidence_service.scan_provisional_skills.return_value = {
        agentic[1].id: {"unknown_skill_id": str(agentic[1].id), "excerpt": "agentic workflows"}}
    with patch(f"{V2}.ProvisionalSkillRepository", return_value=repo), \
            patch(f"{V2}.redacted_resume_text", return_value="redacted text") as text:
        inputs = service.provisional_inputs(JD, RESUME, persist=False)

    text.assert_called_once_with(RESUME)
    service.evidence_service.scan_provisional_skills.assert_called_once_with(RESUME.id, "redacted text", persist=False)
    assert inputs[0].found is True and inputs[0].scanned is True


def test_a_failed_rescan_scores_the_skill_as_not_found_and_says_it_wasnt_scanned():
    agentic = _pending("Agentic Workflows")
    service, repo = _service([agentic], None)
    with patch(f"{V2}.ProvisionalSkillRepository", return_value=repo), \
            patch(f"{V2}.redacted_resume_text", side_effect=RuntimeError("storage down")):
        inputs = service.provisional_inputs(JD, RESUME)

    assert inputs[0].found is False and inputs[0].scanned is False


def test_no_pending_skills_means_no_provisional_inputs():
    service, repo = _service([], None)
    with patch(f"{V2}.ProvisionalSkillRepository", return_value=repo):
        assert service.provisional_inputs(JD, RESUME) == []
    repo.get_scan.assert_not_called()


def test_detection_scans_the_redacted_text_and_stores_what_it_scanned_and_found():
    agentic, other = ProvisionalTerm(uuid4(), "Agentic Workflows"), ProvisionalTerm(uuid4(), "Prompt Design")
    repo = MagicMock()
    repo.all_pending_terms.return_value = [agentic, other]
    detection = MagicMock()
    detection.load_scan_rules.return_value = {}
    service = SkillEvidenceService(detection, MagicMock())
    with patch("app.repositories.provisional_skill_repository.ProvisionalSkillRepository", return_value=repo):
        found = service.scan_provisional_skills(RESUME.id, "Summary\nDesigned agentic workflows for claims.")

    assert list(found) == [agentic.unknown_skill_id]
    kwargs = repo.replace_scan.call_args.kwargs
    assert kwargs["scanned_ids"] == [agentic.unknown_skill_id, other.unknown_skill_id]
    assert kwargs["hits"][0]["excerpt"] == "Designed agentic workflows for claims."
    assert len(kwargs["text_hash"]) == 64          # the text itself is never stored
