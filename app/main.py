import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from app.core.config import settings
from app.db.session import SessionLocal
from app.repositories.embedding_model_version_repository import EmbeddingModelVersionRepository
from app.api.routes import candidate_actions_routes
from app.api.routes import dashboard_routes
from app.api.routes import test_routes
from app.api.routes.jd_routes import router
from app.api.routes import campaign_routes
from app.api.routes.campaign_candidate import router as campaign_candidate_router
from app.api.routes.candidate_routes import router as candidate_router
from app.api.routes.skill_routes import router as skill_router
from app.api.routes import skill_ontology_routes
from app.api.routes.resume_routes import router as resume_router
from app.api.routes.bulk_upload_routes import router as bulk_upload_router
from app.api.routes.monitoring_routes import router as monitoring_router
from app.api.routes import unknown_skill_routes
from app.api.routes import unknown_skill_suggestion_routes
from app.api.routes.prompt_template_routes import router as prompt_template_router
from app.api.routes.dead_letter_routes import router as dead_letter_router
from app.api.routes.talent_pool_routes import filters_router as talent_pool_filters_router
from app.api.routes.talent_pool_routes import router as talent_pool_router
from app.api.routes.audit_log_routes import router as audit_log_router
from app.api.routes.google_oauth_routes import router as google_oauth_router
from app.api.routes.interview_routes import router as interview_router
from app.api.routes.oauth_routes import router as oauth_router
from app.middleware.jwt_middleware import JWTMiddleware
from app.enums.constants import API_PREFIX
from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.exceptions.campaign_exceptions import CampaignException
from app.exceptions.candidate_exceptions import CandidateErasureBlockedException
from app.exceptions.resume_exceptions import ResumeException
from app.exceptions.storage_exception import StorageException
from app.exception_handler.handlers import (
    duplicate_jd_exception_handler,
    campaign_exception_handler,
    http_exception_handler,
    resume_exception_handler,
    candidate_erasure_blocked_exception_handler,
    storage_exception_handler,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="AI Resume Screening Platform (AIRS)",
    description="Secure API with JWT & RBAC",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)


@app.on_event("startup")
def _recover_stalled_resume_uploads_on_startup() -> None:
    """
    Resume-upload resilience: recovers any RESUME_DOCUMENT_PROCESSING
    upload whose Celery dispatch failed while this (or another) app
    instance was previously running with the broker unreachable. Calls
    the recovery logic directly (its own DB session, not a Celery
    dispatch) so it still works even if Celery/Redis are themselves down
    right now - the periodic Beat task (recover-stalled-resume-uploads)
    is the safety net for that case. Best-effort: a failure here (e.g. DB
    unreachable at boot) is logged, never prevents the app from starting.
    """
    from app.tasks.resume_processing_tasks import recover_stalled_resume_uploads
    try:
        recovered = recover_stalled_resume_uploads()
        logger.info("Startup resume-upload recovery scan completed | recovered=%s", recovered)
    except Exception:
        logger.exception("Startup resume-upload recovery scan failed")


app.add_middleware(JWTMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
    max_age=3600,
)


@app.middleware("http")
async def add_timing_middleware(request: Request, call_next):
    t_start = time.time()
    path = request.url.path
    method = request.method

    logger.info("REQUEST START: %s %s", method, path)

    response = await call_next(request)

    elapsed = (time.time() - t_start) * 1000
    response.headers["X-Response-Time"] = f"{elapsed:.2f}ms"

    logger.info("REQUEST END: %s %s - %.2fms - Status: %s", method, path, elapsed, response.status_code)

    if elapsed > 1000:
        logger.error("VERY SLOW REQUEST: %s %s took %.2fms", method, path, elapsed)
    elif elapsed > 500:
        logger.warning("SLOW REQUEST: %s %s took %.2fms", method, path, elapsed)

    return response


def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    openapi_schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }

    for path in openapi_schema["paths"]:
        for method in openapi_schema["paths"][path]:
            if method in ["get", "post", "put", "delete", "patch"]:
                openapi_schema["paths"][path][method]["security"] = [{"BearerAuth": []}]

    app.openapi_schema = openapi_schema
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]

app.include_router(test_routes.router)


@app.on_event("startup")
def sync_embedding_model_version() -> None:
    """
    Mirrors settings.embedding_model (.env) into the active
    embedding_model_versions row on every boot, so EMBEDDING_MODEL is the
    only place anyone needs to change the model — the DB row it drives can
    never be missing or drift out of sync with it.
    """
    db = SessionLocal()
    try:
        version = EmbeddingModelVersionRepository(db).sync_active_from_settings(settings.embedding_model)
        logger.info(
            "Embedding model version synced | model_name=%s model_version=%s id=%s",
            version.model_name, version.model_version, version.id,
        )
    finally:
        db.close()


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "AIRS"}


app.include_router(router=router, prefix=API_PREFIX, tags=["Job Descriptions"])
app.include_router(router=campaign_routes.router, prefix=API_PREFIX, tags=["Campaigns"])
app.include_router(router=campaign_candidate_router, prefix=API_PREFIX, tags=["Campaign Candidates"])
app.include_router(router=candidate_router, prefix=API_PREFIX, tags=["Candidates"])
app.include_router(router=skill_router, prefix=API_PREFIX, tags=["Skill Ontology"])
app.include_router(router=skill_ontology_routes.router, prefix=API_PREFIX, tags=["Skill Ontology"])
app.include_router(router=resume_router, prefix=API_PREFIX, tags=["Resume Intake"])
app.include_router(router=bulk_upload_router, prefix=API_PREFIX, tags=["Bulk Resume Upload"])
app.include_router(router=monitoring_router, prefix=API_PREFIX, tags=["Ops Monitoring"])
app.include_router(router=unknown_skill_suggestion_routes.router, prefix=API_PREFIX, tags=["Unknown Skill Suggestions"])
app.include_router(router=unknown_skill_routes.router, prefix=API_PREFIX, tags=["Unknown Skills"])
app.include_router(router=prompt_template_router, prefix=API_PREFIX, tags=["Prompt Templates"])
app.include_router(router=dead_letter_router, prefix=API_PREFIX, tags=["Dead Letter Queue"])
app.include_router(router=talent_pool_router, prefix=API_PREFIX, tags=["Talent Pool"])
app.include_router(router=audit_log_router, prefix=API_PREFIX, tags=["Audit Log"])
app.include_router(router=talent_pool_filters_router, prefix=API_PREFIX, tags=["Talent Pool"])
app.include_router(router=dashboard_routes.router, prefix=API_PREFIX, tags=["Dashboard"])
app.include_router(router=candidate_actions_routes.router, prefix=API_PREFIX, tags=["Candidate Actions"])
app.include_router(router=interview_router, prefix=API_PREFIX, tags=["Interview Scheduling"])
app.include_router(router=oauth_router, prefix=API_PREFIX, tags=["Microsoft OAuth"])
app.include_router(router=google_oauth_router, prefix=API_PREFIX, tags=["Google OAuth"])

app.add_exception_handler(DuplicateJDException, duplicate_jd_exception_handler)
app.add_exception_handler(CampaignException, campaign_exception_handler)
app.add_exception_handler(ResumeException, resume_exception_handler)
app.add_exception_handler(CandidateErasureBlockedException, candidate_erasure_blocked_exception_handler)
app.add_exception_handler(StorageException, storage_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
