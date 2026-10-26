"""
Audit entries never contain personal data - ids, counts, codes and flags only;
no names, emails, phones, file names, or free text people type (it routinely
quotes a candidate), and never the AI's written assessment.

  - static guard: every audit log call in app/ - its details dict literal, or
    the dict a variable holds when the details are built first - may not use a
    personal-data key, nor take a value from a person's name, email, phone,
    file name, a typed reason/note, decrypted data or the AI response;
  - runtime safety net: AuditService.log drops those keys at any depth.
No database, no email.
"""
import ast
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.audit_service import PERSONAL_DATA_KEYS, AuditService, scrub_personal_data

APP = Path(__file__).resolve().parents[2] / "app"

# value sources that are personal data
PERSONAL_SOURCES = re.compile(
    r"\b(full_name|candidate_name|recipient_name|recipient_email|email|phone|original_filename|filename"
    r"|ranked_by_name|ai_response|decrypt)\b"
    r"|\brequest\.(reason|note|notes|comment)\b"
    r"|\b(final_error_message|full_error_trace|decision_reason|override_reason)\b"
)


# a field of the AI response that is a number or a code, not its written text
_AI_RESPONSE_SAFE_FIELDS = {"scores", "overall_score", "confidence_score", "recommendation"}


def _is_safe_derivation(value) -> bool:
    """bool(x) / len(x) never carry the text; a numeric field of the AI response is a score."""
    if isinstance(value, ast.Call) and getattr(value.func, "id", None) in ("bool", "len"):
        return True
    fields, node = [], value
    while isinstance(node, ast.Subscript):
        if not isinstance(node.slice, ast.Constant):
            return False
        fields.append(node.slice.value)
        node = node.value
    return isinstance(node, ast.Name) and node.id == "ai_response" and bool(fields) and         set(fields) <= _AI_RESPONSE_SAFE_FIELDS


def _audit_calls(tree):
    """(call, enclosing function) for every audit log call."""
    parents = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        keywords = {k.arg: k.value for k in node.keywords}
        if name == "AuditLog" or (name in ("log", "_audit") and "action_type" in keywords):
            scope = node
            while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = parents[scope]
            yield node, keywords, scope


def _dicts_for(details, scope):
    """The dict literals that make up `details`: inline, or assigned/updated on the variable in its function."""
    if isinstance(details, ast.Dict):
        return [details], []
    names = {n.id for n in ast.walk(details) if isinstance(n, ast.Name)}
    dicts, items = [], []
    for node in ast.walk(scope):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in names:
                    dicts += [d for d in ast.walk(node.value) if isinstance(d, ast.Dict)]
                if (isinstance(target, ast.Subscript) and isinstance(target.value, ast.Name)
                        and target.value.id in names and isinstance(target.slice, ast.Constant)):
                    items.append((target.slice.value, node.value))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "update"
                and isinstance(node.func.value, ast.Name) and node.func.value.id in names):
            dicts += [d for a in node.args for d in ast.walk(a) if isinstance(d, ast.Dict)]
    dicts += [d for d in ast.walk(details) if isinstance(d, ast.Dict)]
    return dicts, items


def _violations(path: Path):
    src = path.read_text(encoding="utf-8-sig")
    out = []
    for call, keywords, scope in _audit_calls(ast.parse(src)):
        details = keywords.get("details") or keywords.get("detail")
        if details is None:
            continue
        dicts, items = _dicts_for(details, scope)
        pairs = [(k.value, v) for d in dicts for k, v in zip(d.keys, d.values) if isinstance(k, ast.Constant)]
        pairs += items
        if not dicts and not items:
            pairs.append(("<details>", details))
        for key, value in pairs:
            source = ast.get_source_segment(src, value) or ""
            if key in PERSONAL_DATA_KEYS:
                out.append(f"{path.name}:{call.lineno} key {key!r}")
            elif PERSONAL_SOURCES.search(source) and not _is_safe_derivation(value):
                out.append(f"{path.name}:{call.lineno} {key!r} <- {source[:60]}")
    return out


def test_no_audit_call_site_writes_personal_data():
    offenders = [v for path in sorted(APP.rglob("*.py")) for v in _violations(path)]
    assert offenders == [], (
        "Audit details must hold ids, counts, codes and flags only - record e.g. reason_provided=bool(reason), "
        f"never the text, a name, email, phone or file name: {offenders}")


def test_the_guard_catches_each_kind_of_leak(tmp_path):
    sample = tmp_path / "sample.py"
    sample.write_text('''
def a(self, job_file, request, candidate):
    self.audit_service.log(action_type="X", details={"original_filename": job_file.original_filename})
    self.audit_service.log(action_type="X", details={"title": f"Moved {candidate.full_name}"})
    self.audit_service.log(action_type="X", details={"why": request.reason})

def b(self, ai_response):
    details = {"score": 1}
    details["raw"] = ai_response
    self.audit_service.log(action_type="X", details=details)

def c(self, reason, ai_response):
    self.audit_service.log(action_type="X", details={"reason_provided": bool(reason), "count": 3,
                                                       "reason_code": "ALL_POSITIONS_FILLED",
                                                       "score": ai_response["scores"]["overall_score"]})
    self.audit_service.log(action_type="X", details={"text": ai_response["summary"]})
''')
    found = _violations(sample)
    assert len(found) == 5, found
    assert sum(":13" in v for v in found) == 0 and any(":16" in v for v in found), found


# --- runtime safety net --------------------------------------------------------------------

def test_scrub_drops_personal_keys_at_any_depth_without_mutating():
    details = {"resume_id": "r-1", "original_filename": "Jane_CV.pdf",
               "nested": {"email": "jane@example.invalid", "count": 2},
               "items": [{"reason": "spoke to Jane", "stage": "SCREENING"}]}
    cleaned, dropped = scrub_personal_data(details)
    assert cleaned == {"resume_id": "r-1", "nested": {"count": 2}, "items": [{"stage": "SCREENING"}]}
    assert dropped == ["email", "original_filename", "reason"]
    assert details["original_filename"] == "Jane_CV.pdf"                      # input untouched


def test_product_names_are_kept():
    cleaned, dropped = scrub_personal_data({"name": "Backend Engineer preset", "title": "Campaign 'X' closed"})
    assert dropped == [] and cleaned["name"] == "Backend Engineer preset"


def test_audit_service_log_writes_the_scrubbed_details_and_warns():
    repo = MagicMock()
    service = AuditService.__new__(AuditService)
    service.audit_repo = repo
    service.repository = repo
    with patch("app.services.audit_service.logger") as logger:
        audit = service.log(actor_id="hr-1", actor_role="HR_ADMIN", action_type="RESUME_UPLOADED",
                            entity_type="RESUME", entity_id="r-1",
                            details={"resume_id": "r-1", "original_filename": "Jane_CV.pdf"})
    written = repo.create.call_args.args[0] if repo.create.called else audit
    assert written.detail == {"resume_id": "r-1"}
    assert "original_filename" in str(logger.warning.call_args) and "Jane_CV" not in str(logger.warning.call_args)


@pytest.fixture(autouse=True)
def no_email():
    with patch("app.services.notifications.ses_email_client.SESEmailClient.send_email",
               side_effect=AssertionError("email sent")):
        yield


# --- system reason codes are kept; free text and resume excerpts are not ---------------------------

def test_known_reason_codes_are_kept_at_any_depth_and_free_text_is_dropped():
    details = {"results": [{"skill": "Python", "reason": "all_votes_yes"},
                           {"skill": "Git", "reason": "spoke to Jane - she uses it daily"}],
               "reason": "daily_llm_cap_reached"}
    cleaned, dropped = scrub_personal_data(details)
    assert cleaned == {"results": [{"skill": "Python", "reason": "all_votes_yes"}, {"skill": "Git"}],
                       "reason": "daily_llm_cap_reached"}
    assert dropped == ["reason"]


def test_the_known_codes_cover_every_confirmation_reason():
    from app.services.audit_service import KNOWN_REASON_CODES
    from app.services.skills import core_skill_confirmation_service as confirmation
    emitted = {value for name, value in vars(confirmation).items() if name.startswith("REASON_")}
    assert emitted and emitted <= KNOWN_REASON_CODES, emitted - KNOWN_REASON_CODES


def test_resume_excerpts_are_dropped():
    details = {"deterministic_score": 72, "score_breakdown": {"v2": {"results": [{"quote": "Led the ..."}]}},
               "items": [{"line": "Built REST APIs at ...", "evidence_text": "..."}]}
    cleaned, dropped = scrub_personal_data(details)
    assert cleaned == {"deterministic_score": 72, "items": [{}]}
    assert dropped == ["evidence_text", "line", "score_breakdown"]


def test_the_deterministic_audit_entry_carries_scores_not_the_breakdown():
    src = (APP / "tasks" / "deterministic_scoring_tasks.py").read_text(encoding="utf-8")
    payload = src[src.index("summary_payload = {"):src.index("}", src.index("summary_payload = {"))]
    assert '"score_breakdown"' not in payload and '"deterministic_score"' in payload
