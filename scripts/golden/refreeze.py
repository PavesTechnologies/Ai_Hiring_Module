"""
Freeze (or refreeze) golden-set resumes: parse each case's source .docx with the app's own parse stages -
text extraction, cleaning, PII detection and redaction, the AI extraction (the JD campaign's parse prompt,
no parse cache), JSON validation and skill normalization - and compare with the frozen file. Shows the
diff (text lines, parsed fields, matched skills) and writes only with --write. Writes nothing to the
database except the LLM call log (source "golden_refreeze"). One LLM call per resume.

    python -m scripts.golden.refreeze --only CF-NAME-F1-2 CF-GAP-S7-1      show the diffs
    python -m scripts.golden.refreeze --only CF-NAME-F1-2 --write          ... and write them
    python -m scripts.golden.refreeze --missing --write                    every case not frozen yet
"""
import argparse
import datetime
import difflib
import hashlib
import json
import sys
import uuid
from pathlib import Path

from scripts.golden.golden_format import GOLDEN_DIR, ROOT, load_json

RESUME_KEEP = ("file_format", "page_count", "parse_status", "is_active_version", "version_number")


def diff_frozen(old: dict | None, new: dict) -> list[str]:
    """Human-readable differences between two frozen resumes (empty when they are the same)."""
    if old is None:
        return ["(new - not frozen before)"]
    lines = []
    text = [f"  text {line}" for line in difflib.unified_diff(
        old.get("redacted_text", "").splitlines(), new.get("redacted_text", "").splitlines(), lineterm="", n=0)
        if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    lines += text
    old_parse, new_parse = old.get("parsed_json") or {}, new.get("parsed_json") or {}
    for key in sorted(set(old_parse) | set(new_parse)):
        a, b = old_parse.get(key), new_parse.get(key)
        if a == b:
            continue
        if isinstance(a, list) and isinstance(b, list):
            lines += [f"  parsed {key}: {line}" for line in _list_diff(a, b)]
        else:
            lines.append(f"  parsed {key}: {_short(a, 120)} -> {_short(b, 120)}")
    skill = lambda s: (s.get("skill"), s.get("match_tier"), s.get("raw_extracted_text"))  # noqa: E731
    old_skills, new_skills = {skill(s) for s in old.get("candidate_skills") or []}, \
        {skill(s) for s in new.get("candidate_skills") or []}
    lines += [f"  skill - {s}" for s in sorted(old_skills - new_skills, key=str)]
    lines += [f"  skill + {s}" for s in sorted(new_skills - old_skills, key=str)]
    return lines


# What identifies an entry in a parsed list (a role, a degree, a certification...).
_ENTRY_KEYS = ("title", "company", "start_date", "degree", "institution", "name", "skill")


def _short(value, limit: int = 160) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)[:limit]


def _list_diff(old: list, new: list) -> list[str]:
    """Entries removed or added, and for the same entry (same identifying fields) the fields that changed."""
    if not all(isinstance(x, dict) for x in old + new):
        gone = [x for x in old if x not in new]
        added = [x for x in new if x not in old]
        return [f"- {_short(x)}" for x in gone] + [f"+ {_short(x)}" for x in added]
    ident = lambda e: tuple((k, e.get(k)) for k in _ENTRY_KEYS if e.get(k) is not None)  # noqa: E731
    old_by, new_by = {ident(e): e for e in old}, {ident(e): e for e in new}
    out = [f"- {_short(e)}" for k, e in old_by.items() if k not in new_by]
    out += [f"+ {_short(e)}" for k, e in new_by.items() if k not in old_by]
    for k in old_by.keys() & new_by.keys():
        a, b = old_by[k], new_by[k]
        label = " / ".join(str(v) for _, v in k)[:60]
        added_empty = sorted(f for f in set(b) - set(a) if b[f] in (None, False, "", [], {}))
        changed = sorted(f for f in set(a) | set(b) if a.get(f) != b.get(f) and f not in added_empty)
        if added_empty:
            out.append(f"~ {label}: fields added, empty or false: {', '.join(added_empty)}")
        for field in changed:
            out.append(f"~ {label}: {field}: {_short(a.get(field), 80)} -> {_short(b.get(field), 80)}")
    return out


def parse_docx(db, docx: Path, prompt_name: str) -> dict:
    """The frozen form of one resume, parsed now with the app's parse stages (one LLM call)."""
    from sqlalchemy import select
    from app.core.redis_client import get_redis_client
    from app.models.prompt_template import PromptTemplate, PromptTemplateStatus
    from app.models.skills import SkillOntology
    from app.models.resume.resume_source_format import ResumeSourceFormat
    from app.repositories.prompt_template_repository import PromptTemplateRepository
    from app.repositories.skill_repository import SkillRepository
    from app.services.ai.embedding_service import EmbeddingService
    from app.services.ai.preprocessing_service import PreprocessingService
    from app.services.cache_service import CacheService
    from app.services.extractions.llm_extraction_service import LLMExtractionService
    from app.services.llm.call_log import llm_call_source
    from app.services.llm.factory import resolve_active_provider
    from app.services.llm.model_selection import LLMPurpose
    from app.services.pii.pii_detection_service import PIIDetectionService
    from app.services.pii.pii_redaction_service import PIIRedactionService
    from app.services.resume.resume_processing_context import ResumeProcessingContext
    from app.services.resume.resume_processing_pipeline import ResumeProcessingPipeline
    from app.services.resume.resume_text_extraction_service import ResumeTextExtractionService
    from app.services.skills.skill_normalization_service import (
        SkillNormalizationService, scoring_weight_for_tier, verification_status_for_tier,
    )

    template = db.execute(select(PromptTemplate).where(PromptTemplate.name == prompt_name,
                                                       PromptTemplate.status == PromptTemplateStatus.ACTIVE)).scalars().first()
    if template is None:
        raise SystemExit(f"No active prompt template named {prompt_name!r}")
    skill_repo = SkillRepository(db)
    pipeline = ResumeProcessingPipeline(
        preprocessing_service=PreprocessingService(),
        extraction_service=LLMExtractionService(resolve_active_provider(db, LLMPurpose.EXTRACTION)),
        storage_service=None, skill_normalization_service=SkillNormalizationService(
            skill_repo, EmbeddingService(db), cache_service=CacheService(get_redis_client())),
        resume_service=None, resume_repository=None, skill_repository=skill_repo, stage_tracker=None,
        pii_detection_service=PIIDetectionService(), pii_redaction_service=PIIRedactionService(),
        prompt_template_repository=PromptTemplateRepository(db),
    )
    context = ResumeProcessingContext(task_id=f"golden-{uuid.uuid4()}", file_path=str(docx),
                                      source_format=ResumeSourceFormat.DOCX)
    context.prompt_template_id = template.id
    context.raw_text = ResumeTextExtractionService.extract(docx.read_bytes(), ResumeSourceFormat.DOCX)
    pipeline._run_text_cleaning(context)
    pipeline._run_pii_detection(context)
    pipeline._run_pii_redaction(context)                # no resume id: records no text hash
    with llm_call_source("golden_refreeze"):
        pipeline._run_ai_extraction(context)            # no detection repository: no parse cache
    pipeline._run_json_validation(context)
    pipeline._run_skill_normalization(context)

    names = dict(db.execute(select(SkillOntology.id, SkillOntology.canonical_name)).all())
    seen, skills = set(), []
    for match in context.skill_match_results or []:      # one row per canonical skill, like persistence
        if match.canonical_skill_id and match.canonical_skill_id in seen:
            continue
        seen.add(match.canonical_skill_id)
        skills.append({"skill": names.get(match.canonical_skill_id), "raw_extracted_text": match.raw_text,
                       "confidence": match.confidence, "match_tier": match.match_tier.value,
                       "status": verification_status_for_tier(match.match_tier).value,
                       "scoring_weight": scoring_weight_for_tier(match.match_tier)})
    text = context.redacted_text
    call = context.parse_call or {}
    return {
        "redacted_text": text, "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "parsed_json": context.validated_extraction.model_dump(mode="json"),
        "candidate_skills": sorted(skills, key=lambda s: (s["skill"] or "", s["raw_extracted_text"] or "")),
        "parse": {"source": "refreeze", "prompt_template": prompt_name, "provider": call.get("provider"),
                  "model": call.get("model"), "frozen_on": datetime.date.today().isoformat()},
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--only", nargs="+", metavar="CASE")
    which.add_argument("--missing", action="store_true", help="every case with no frozen resume yet")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    import logging
    logging.disable(logging.WARNING)
    from app.core.config import settings
    if settings.email_delivery != "log":
        print("EMAIL_DELIVERY must be log - refusing to run.")
        return 2
    import app.models  # noqa: F401
    from app.db.session import SessionLocal, engine

    golden = json.loads((GOLDEN_DIR / "golden_set.json").read_text(encoding="utf-8"))
    cases = {c["id"]: c for c in golden["cases"]}
    if args.only:
        unknown = [c for c in args.only if c not in cases]
        if unknown:
            print("unknown cases:", unknown)
            return 2
        chosen = [cases[c] for c in args.only]
    else:
        chosen = [c for c in golden["cases"] if not (GOLDEN_DIR / c["resume"]).exists()]
    engine.echo = False
    db = SessionLocal()
    changed = 0
    try:
        for case in chosen:
            jd = load_json(golden["jds"][case["jd"]])
            frozen_path = GOLDEN_DIR / case["resume"]
            old = json.loads(frozen_path.read_text(encoding="utf-8")) if frozen_path.exists() else None
            new = {"id": case["id"], "jd": case["jd"], "source_docx": case["source_docx"],
                   "original_filename": Path(case["source_docx"]).name,
                   **parse_docx(db, ROOT / case["source_docx"], jd["campaign"]["prompt_template"]),
                   "resume": {k: v for k, v in ((old or {}).get("resume") or {"file_format": "DOCX",
                              "parse_status": "PARSED", "is_active_version": True, "version_number": 1}).items()
                              if k in RESUME_KEEP},
                   "candidate": (old or {}).get("candidate") or {"jurisdiction": "GLOBAL"}}
            differences = diff_frozen(old, new)
            print(f"== {case['id']}: {'no change' if not differences else f'{len(differences)} difference(s)'}")
            for line in differences:
                print(line)
            if differences:
                changed += 1
                if args.write:
                    frozen_path.write_text(json.dumps(new, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
                    print("   written")
        print(f"{len(chosen)} parsed, {changed} with differences" + ("" if args.write else " - nothing written"))
        return 0
    finally:
        db.rollback()
        db.close()


if __name__ == "__main__":
    sys.exit(main())
