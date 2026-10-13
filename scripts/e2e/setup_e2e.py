"""
Prepares the ISOLATED e2e database (run inside the e2e stack - see e2e/README.md):

    docker compose -f e2e/docker-compose.e2e.yml run --rm tools -m scripts.e2e.setup_e2e

Refuses to run unless every setting points at the local e2e containers.
Idempotent: migrations, seeds, test users, the skill catalogue, a copy of
the "AI transformation lead Sep" JD, and two test campaigns (A, and B which
shares candidate resumes for the out-of-date check).
"""
import hashlib
import json
import logging
import subprocess
import sys
import uuid
from pathlib import Path

logging.basicConfig(level=logging.WARNING)

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "e2e"
LIVE_MARKERS = ("amazonaws.com", "supabase.co", "cloudfront.net")

USERS = {
    "e2e-hr-admin": ("hr.admin@e2e.invalid", "HR_ADMIN", "E2E HR Admin"),
    "e2e-recruiter": ("recruiter@e2e.invalid", "RECRUITER", "E2E Recruiter"),
    "e2e-hiring-manager": ("hiring.manager@e2e.invalid", "HIRING_MANAGER", "E2E Hiring Manager"),
}
CAMPAIGNS = {"A": "[E2E] AI transformation lead", "B": "[E2E] AI transformation lead - shared resume"}
JD_TITLE_PREFIX = "[E2E] "
SEEDS = ("seed_encryption_key", "seed_encryption_key_ai_provider", "seed_encryption_key_oauth_token",
         "seed_platform_config", "seed_allowed_transitions", "seed_prompt_templates")


def assert_isolated() -> None:
    """Hard stop if anything could reach a live system."""
    from app.core.config import settings
    problems = []
    if settings.db_host != "postgres":
        problems.append("database host is not the local e2e container")
    if (settings.email_delivery or "").lower() != "log":
        problems.append("EMAIL_DELIVERY is not log")
    if (settings.storage_backend or "").lower() != "local":
        problems.append("STORAGE_BACKEND is not local")
    for name, value in settings.model_dump().items():
        if isinstance(value, str) and any(marker in value for marker in LIVE_MARKERS):
            problems.append(f"setting {name} points at a live host")
    if problems:
        raise SystemExit("REFUSING TO RUN - not isolated: " + "; ".join(problems))
    print("isolation check: OK (local database, log-only email, local storage, no live hosts)")


def phase2_jd_title(fixture: dict) -> str:
    return JD_TITLE_PREFIX + fixture["jd"]["title"] + fixture["phase2"]["title_suffix"]


def _create_jd(db, *, data: dict, title: str, raw_text: str, extracted_json: dict, jd_skill_rows: list,
               skills: dict, jd_prompt, is_verified: str):
    """A JD copy from the fixture, with its jd_skills rows (skills by canonical name); embedded."""
    from app.models.jd.job_descriptions import JobDescription
    from app.models.skills import JDSkill, JDSkillImportance, JDSkillVerificationStatus

    jd = JobDescription(
        title=title, raw_text=raw_text, extracted_json=extracted_json,
        required_skills=data["required_skills"] or {}, min_experience_years=data["min_experience_years"],
        max_experience_years=data["max_experience_years"], notice_period=data["notice_period"] or 0,
        education_criteria=data["education_criteria"] or {}, source_format=data["source_format"],
        content_hash=hashlib.sha256(raw_text.encode("utf-8")).hexdigest(),
        created_by="e2e-hr-admin", prompt_template_id=jd_prompt.id, jurisdiction=data["jurisdiction"],
        is_verified=is_verified,
    )
    db.add(jd)
    db.flush()
    for row in jd_skill_rows:
        skill = skills.get(row["skill"])
        if skill is None:
            continue
        db.add(JDSkill(
            jd_id=jd.id, canonical_skill_id=skill.id, mandatory=row["mandatory"], weight=row["weight"],
            importance=JDSkillImportance(row["importance"]) if row["importance"] else None,
            alias_skill_ids=[skills[a].id for a in row["aliases"] if a in skills] or None,
            match_tier=row["match_tier"] or "EXACT", confidence=row["confidence"],
            verification_status=JDSkillVerificationStatus(row["verification_status"] or "AUTO_VERIFIED"),
        ))
    db.commit()
    from app.tasks.jd_embedding_tasks import generate_jd_embedding
    generate_jd_embedding.apply(kwargs={"task_id": str(uuid.uuid4()), "jd_id": str(jd.id)})
    return jd


def _seed_phase2_jd(db, fixture: dict, skills: dict, jd_prompt) -> None:
    """
    Phase 2 e2e: a second, UNcalibrated copy of the JD carrying two skills that
    aren't in the catalogue (fixture "phase2"), linked as pending JD unknown
    skills - their tiers come from the copy's extraction, as in production.
    """
    import copy
    from app.models.jd.job_descriptions import JobDescription
    from app.models.skills import JDUnknownSkill, JDUnknownSkillStatus, UnknownSkill

    phase2, data = fixture["phase2"], fixture["jd"]
    title = phase2_jd_title(fixture)
    if db.query(JobDescription).filter(JobDescription.title == title).first() is not None:
        return
    extracted = copy.deepcopy(data["extracted_json"])
    required = extracted.setdefault("required_skills", {})
    for item in phase2["unknown_skills"]:
        required.setdefault(item["tier"].lower(), []).append(item["raw_text"])
    jd = _create_jd(db, data=data, title=title, raw_text=data["raw_text"] + "\n\n" + phase2["raw_text_addition"],
                    extracted_json=extracted, jd_skill_rows=fixture["jd_skills"], skills=skills,
                    jd_prompt=jd_prompt, is_verified="PARTIALLY_VERIFIED")
    for item in phase2["unknown_skills"]:
        unknown = db.query(UnknownSkill).filter(UnknownSkill.raw_text == item["raw_text"]).first()
        if unknown is None:
            unknown = UnknownSkill(raw_text=item["raw_text"], normalized_key=item["raw_text"].lower().replace(" ", "_"))
            db.add(unknown)
            db.flush()
        db.add(JDUnknownSkill(jd_id=jd.id, unknown_skill_id=unknown.id, mandatory=item["tier"] != "PREFERRED",
                              status=JDUnknownSkillStatus.PENDING))
    db.commit()
    print(f"Phase 2 JD: {title} (+{len(phase2['unknown_skills'])} unknown skills, not calibrated)")


def run(module: str, *args: str) -> None:
    result = subprocess.run([sys.executable, "-m", module, *args], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stdout[-2000:], result.stderr[-3000:])
        raise SystemExit(f"{module} failed")


def main() -> None:
    assert_isolated()
    result = subprocess.run(["alembic", "upgrade", "head"], capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr[-3000:])
        raise SystemExit("migrations failed")
    print("migrations: OK")
    for seed in SEEDS:
        run(f"app.seeds.{seed}")
    print("seeds: OK")

    from app.db.session import SessionLocal
    import app.models  # noqa: F401
    from app.core.config import settings
    from app.models.campaigns import HiringCampaign
    from app.api.routes.campaign_routes import SYSTEM_ORG
    from app.models.identity import Organization, User, UserRole
    from app.models.jd.job_descriptions import JobDescription
    from app.models.prompt_template import PromptTemplate
    from app.models.skills import SkillOntology
    from app.repositories.embedding_model_version_repository import EmbeddingModelVersionRepository
    from app.services.ai.embedding_service import EmbeddingService

    db = SessionLocal()
    try:
        EmbeddingModelVersionRepository(db).sync_active_from_settings(settings.embedding_model)
        # POST /campaigns creates every campaign under this org (the live DB has it; an empty one doesn't).
        if db.get(Organization, SYSTEM_ORG) is None:
            db.add(Organization(id=SYSTEM_ORG, name="E2E system organisation"))
            db.flush()
        for user_id, (email, role, name) in USERS.items():
            if db.get(User, user_id) is None:
                db.add(User(id=user_id, email=email, role=UserRole[role], full_name=name,
                            password_hash="EXTERNAL_AUTH", is_active=True))
        db.commit()

        catalogue = json.loads((FIXTURES / "skill_catalogue.json").read_text("utf-8"))
        existing = {s.canonical_name: s for s in db.query(SkillOntology).all()}
        for item in catalogue:
            if item["canonical_name"] not in existing:
                skill = SkillOntology(canonical_name=item["canonical_name"], aliases=item["aliases"] or None,
                                      category=item["category"], confidence=item["confidence"] or "unverified",
                                      source=item["source"], is_active=True)
                db.add(skill)
                existing[skill.canonical_name] = skill
        db.flush()
        for item in catalogue:
            if item["parent"] and item["parent"] in existing:
                existing[item["canonical_name"]].parent_skill_id = existing[item["parent"]].id
        missing = [s for s in existing.values() if s.embedding is None]
        if missing:
            vectors = EmbeddingService(db).generate_embeddings([s.canonical_name for s in missing], batch_size=64)
            for skill, vector in zip(missing, vectors):
                skill.embedding = vector
        db.commit()
        print(f"skill catalogue: {len(existing)} skills ({len(missing)} embedded now)")
        run("app.seeds.seed_scoring_v2")

        fixture = json.loads((FIXTURES / "jd_ai_transformation_lead.json").read_text("utf-8"))
        jd_prompt = db.query(PromptTemplate).filter(PromptTemplate.task_type == "JD_PARSE").first()
        jd = db.query(JobDescription).filter(JobDescription.title == JD_TITLE_PREFIX + fixture["jd"]["title"]).first()
        if jd is None:
            data = fixture["jd"]
            jd = _create_jd(db, data=data, title=JD_TITLE_PREFIX + data["title"], raw_text=data["raw_text"],
                            extracted_json=data["extracted_json"], jd_skill_rows=fixture["jd_skills"],
                            skills=existing, jd_prompt=jd_prompt, is_verified="VERIFIED")
        print(f"JD copy: {jd.title} ({len(fixture['jd_skills'])} skills)")
        if fixture.get("phase2"):
            _seed_phase2_jd(db, fixture, existing, jd_prompt)

        resume_prompt = db.query(PromptTemplate).filter(PromptTemplate.task_type == "RESUME_PARSE").first()
        ai_prompt = db.query(PromptTemplate).filter(PromptTemplate.task_type == "AI_EVALUATE").first()
        settings_from = fixture["campaign"]
        for key, name in CAMPAIGNS.items():
            if db.query(HiringCampaign).filter(HiringCampaign.name == name).first() is None:
                db.add(HiringCampaign(
                    name=name, jd_id=jd.id, prompt_template_id=resume_prompt.id,
                    ai_evaluate_prompt_id=ai_prompt.id if ai_prompt else None,
                    weight_deterministic=settings_from["weight_deterministic"],
                    weight_semantic=settings_from["weight_semantic"], weight_ai=settings_from["weight_ai"],
                    semantic_threshold=settings_from["semantic_threshold"], ai_threshold=settings_from["ai_threshold"],
                    deterministic_threshold=settings_from["deterministic_threshold"],
                    required_skill_coverage_threshold=settings_from["required_skill_coverage_threshold"],
                    max_missing_core_skills=int(settings_from["max_missing_core_skills"]),
                    max_missing_core_pct=settings_from["max_missing_core_pct"],
                    hiring_manager_id="e2e-hiring-manager", recruiter_id="e2e-recruiter", created_by="e2e-hr-admin",
                ))
        db.commit()
        print("campaigns: " + ", ".join(CAMPAIGNS.values()))
    finally:
        db.close()


if __name__ == "__main__":
    main()
