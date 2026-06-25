from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings

app = FastAPI(
    title="AI Resume Screening Platform (AIRS)",
    description="Enterprise-grade AI hiring platform — resume parsing, semantic matching, and pipeline automation.",
    version="1.0.0",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routers (uncomment as routes are implemented) ─────────────────────────────
# from app.api.routes import auth, jobs, campaigns, candidates, resumes
# from app.api.routes import pipeline, search, analytics, skills, admin
# app.include_router(auth.router,       prefix="/api/v1/auth",       tags=["Auth"])
# app.include_router(jobs.router,       prefix="/api/v1/jobs",       tags=["Jobs"])
# app.include_router(campaigns.router,  prefix="/api/v1/campaigns",  tags=["Campaigns"])
# app.include_router(candidates.router, prefix="/api/v1/candidates", tags=["Candidates"])
# app.include_router(resumes.router,    prefix="/api/v1/resumes",    tags=["Resumes"])
# app.include_router(pipeline.router,   prefix="/api/v1/pipeline",   tags=["Pipeline"])
# app.include_router(search.router,     prefix="/api/v1/search",     tags=["Search"])
# app.include_router(analytics.router,  prefix="/api/v1/analytics",  tags=["Analytics"])
# app.include_router(skills.router,     prefix="/api/v1/skills",     tags=["Skills"])
# app.include_router(admin.router,      prefix="/api/v1/admin",      tags=["Admin"])


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "AIRS"}
