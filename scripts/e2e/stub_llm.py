"""
Deterministic stub LLM for the isolated e2e stack - no network, no keys.

Answers the structured calls the e2e flows make:
  - resume extraction (prompt contains the resume text after "Job Description:")
  - core-skill confirmation (CoreSkillCheckResponse)
  - AI evaluation (AIEvaluationGenerationSchema): a fixed SHORTLIST recommendation
Anything else raises, so an unexpected LLM call shows up as a failure.

Every call goes through the normal CountedProvider, so it is logged in llm_call_log (model
e2e-stub-v1) and counts toward the daily cap like a real call. A driver can switch the stub to a
provider outage for core-skill confirmation by writing {"confirm": "outage"} to
e2e_stub_mode.json on the shared storage volume (set_mode below).

Resume extraction reads the fixture layout written by scripts/e2e/export_fixtures.py.
The FIRST parse of a given text returns only part of its competency line
(mimicking the original problem - competencies missed); every later parse of
the same text returns all of it. So a re-parse visibly changes the skills.
Call counts are kept in a file on the shared e2e storage volume.
"""
import hashlib
import json
import re
from pathlib import Path

from app.core.config import settings
from app.services.llm.base import LLMErrorReason, LLMPermanentError, LLMTransientError

_HEADINGS = {"PROFILE", "CORE COMPETENCIES", "PROFESSIONAL EXPERIENCE", "EDUCATION", "CERTIFICATIONS"}
_DATES = re.compile(r"^(\d{4}(?:-\d{2})?)\s*-\s*(\d{4}(?:-\d{2})?|present|current)$", re.I)
_ITEM = re.compile(r"\[(\d+)\] Skill: [^\n]*\n\s+Excerpts:\n((?:\s+- .*(?:\n|$))+)")
_EXCERPT = re.compile(r'- \[([^\]]*)\] "(.*)"')
FIRST_PARSE_COMPETENCIES = 3


def _counter_file() -> Path:
    return Path(settings.local_storage_dir) / "e2e_stub_llm_calls.json"


def _mode_file() -> Path:
    return Path(settings.local_storage_dir) / "e2e_stub_mode.json"


def mode() -> dict:
    path = _mode_file()
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


def set_mode(**values) -> None:
    """e.g. set_mode(confirm="outage"), set_mode(confirm="ok", last_attempt_fallback=False)."""
    path = _mode_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**mode(), **values}), "utf-8")


def _bump(key: str) -> int:
    path = _counter_file()
    counts = json.loads(path.read_text("utf-8")) if path.exists() else {}
    counts[key] = counts.get(key, 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(counts), "utf-8")
    return counts[key]


def parse_resume(text: str, call_no: int) -> dict:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    section, skills, jobs, education, summary = None, [], [], [], []
    name = lines[0] if lines else None
    for line in lines[1:]:
        if line.upper() in _HEADINGS:
            section = line.upper()
            continue
        if section == "PROFILE":
            summary.append(line)
        elif section == "CORE COMPETENCIES":
            items = [item.strip() for item in line.split("|") if item.strip()]
            skills.extend(items if call_no > 1 else items[:FIRST_PARSE_COMPETENCIES])
        elif section == "PROFESSIONAL EXPERIENCE":
            dates = _DATES.match(line)
            if dates and jobs:
                end = dates.group(2)
                jobs[-1].update(start_date=dates.group(1), end_date=None if end.lower() in ("present", "current") else end,
                                is_current=end.lower() in ("present", "current"))
            elif " - " in line and len(line) < 140:
                title, company = line.split(" - ", 1)
                jobs.append({"title": title.strip(), "company": company.strip()})
            elif jobs:
                jobs[-1]["description"] = ((jobs[-1].get("description") or "") + " " + line).strip()
        elif section == "EDUCATION":
            parts = [p.strip() for p in line.split(",")]
            year = next((int(p) for p in parts if p.isdigit() and len(p) == 4), None)
            education.append({"degree": parts[0] if parts else None, "field": parts[1] if len(parts) > 2 else None,
                              "institution": next((p for p in parts if p.startswith("University")), None),
                              "graduation_year": year})
    return {"full_name": name, "summary": " ".join(summary)[:1500] or None, "skills": skills,
            "work_experience": jobs, "education": education}


def confirm(prompt: str) -> dict:
    items = []
    for match in _ITEM.finditer(prompt):
        excerpts = _EXCERPT.findall(match.group(2))
        own = [(label, text) for label, text in excerpts if label.lower() not in ("recognition", "events", "interests")]
        if own:
            items.append({"number": int(match.group(1)), "present": True, "quote": " ".join(own[0][1].split()[:6])})
        else:
            items.append({"number": int(match.group(1)), "present": False, "quote": ""})
    return {"items": items}


class E2EStubProvider:
    provider_name = "E2E_STUB"
    model = "e2e-stub-v1"
    last_model_used = "e2e-stub-v1"
    last_used_fallback = False

    def generate_json(self, prompt: str, response_schema, *, deterministic: bool = False) -> dict:
        name = getattr(response_schema, "__name__", "")
        if name == "CoreSkillCheckResponse":
            if mode().get("confirm") == "outage":
                raise LLMTransientError("e2e stub: provider outage", reason=LLMErrorReason.UNAVAILABLE)
            return confirm(prompt)
        if name == "AIEvaluationGenerationSchema":
            return {"scores": {"technical_match": 80, "experience_match": 75, "education_match": 70,
                               "domain_match": 75, "overall_score": 76},
                    "confidence_score": 80, "recommendation": "SHORTLIST",
                    "strengths": ["e2e stub"], "gaps": []}
        if name.startswith("ResumeExtraction"):
            text = prompt.split("Job Description:", 1)[-1]
            key = hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()
            return parse_resume(text, _bump(key))
        raise LLMPermanentError(f"e2e stub LLM has no answer for {name}", reason=LLMErrorReason.BAD_REQUEST)

    def list_models(self):
        return []


def install() -> None:
    """Routes every resolve_active_provider / resolve_provider lookup (already-imported names too) to the stub."""
    import sys
    from app.services.llm import factory

    from app.services.llm.call_log import CountedProvider
    # Logged in llm_call_log like any provider, so the daily cap and the reports see the calls.
    stub = lambda *args, **kwargs: CountedProvider(E2EStubProvider())  # noqa: E731
    factory.resolve_active_provider = stub
    factory.resolve_provider = stub
    for module in list(sys.modules.values()):
        for attr in ("resolve_active_provider", "resolve_provider"):
            if module is not None and getattr(module, attr, None) is not None and module is not factory:
                try:
                    setattr(module, attr, stub)
                except (AttributeError, TypeError):
                    pass
