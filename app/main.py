from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes.jd_routes import router as jd_router
from app.api.routes.campaign_routes import router as campaign_router

from app.core.config import settings
from app.enums.constants import API_PREFIX

from app.exceptions.duplicate_jd_exception import DuplicateJDException
from app.exceptions.campaign_exceptions import CampaignException
from app.exception_handler.handlers import (
    duplicate_jd_exception_handler,
    campaign_exception_handler,
    http_exception_handler,
)

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


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "AIRS"}


app.include_router(router=jd_router, prefix=API_PREFIX, tags=["Job Descriptions"])
app.include_router(router=campaign_router, prefix=API_PREFIX, tags=["Campaigns"])


app.add_exception_handler(DuplicateJDException, duplicate_jd_exception_handler)
app.add_exception_handler(CampaignException, campaign_exception_handler)
app.add_exception_handler(HTTPException, http_exception_handler)
