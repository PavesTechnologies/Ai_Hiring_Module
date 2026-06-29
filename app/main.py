import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.constants import API_PREFIX
from app.middleware.rbac import RBACMiddleware, preload_jwks
from app.schemas.response import APIResponse

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, preload_jwks)
    except RuntimeError as exc:
        if settings.app_env == "production":
            raise
        logger.warning("JWKS preload failed: %s", exc)
    yield


app = FastAPI(
    title="AI Resume Screening Platform (AIRS)",
    description="Enterprise-grade AI hiring platform.",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

@app.exception_handler(HTTPException)
async def http_exception_handler(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=APIResponse.fail(message=str(exc.detail)).model_dump(),
        headers=getattr(exc, "headers", None),
    )

app.add_middleware(RBACMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers (uncomment as routes are implemented) ─────────────────────────────
# from app.api.routes import jobs, campaigns, candidates, resumes
# from app.api.routes import pipeline, search, analytics, skills, admin
# app.include_router(jobs.router,       prefix=f"{API_PREFIX}/jobs",       tags=["Jobs"])
# app.include_router(campaigns.router,  prefix=f"{API_PREFIX}/campaigns",  tags=["Campaigns"])
# app.include_router(candidates.router, prefix=f"{API_PREFIX}/candidates", tags=["Candidates"])
# app.include_router(resumes.router,    prefix=f"{API_PREFIX}/resumes",    tags=["Resumes"])
# app.include_router(pipeline.router,   prefix=f"{API_PREFIX}/pipeline",   tags=["Pipeline"])
# app.include_router(search.router,     prefix=f"{API_PREFIX}/search",     tags=["Search"])
# app.include_router(analytics.router,  prefix=f"{API_PREFIX}/analytics",  tags=["Analytics"])
# app.include_router(skills.router,     prefix=f"{API_PREFIX}/skills",     tags=["Skills"])
# app.include_router(admin.router,      prefix=f"{API_PREFIX}/admin",      tags=["Admin"])


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "AIRS"}
