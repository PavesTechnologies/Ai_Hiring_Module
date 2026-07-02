import logging
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.api.routes import test_routes
from app.api.routes.jd_routes import router
from app.middleware.jwt_middleware import JWTMiddleware
from app.enums.constants import API_PREFIX
from app.exceptions.duplicate_jd_exception import DuplicateJDException

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

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"status_code": exc.status_code, "message": exc.detail},
        headers=getattr(exc, "headers", None),
    )



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


@app.get("/health", tags=["Health"])
def health():
    return {"status": "ok", "service": "AIRS"}

app.include_router(router=router, prefix="/api/v1", tags=["Job Descriptions"])


@app.exception_handler(DuplicateJDException)
async def duplicate_jd_exception_handler(
    request: Request,
    exc: DuplicateJDException,
):
    return JSONResponse(
        status_code=409,
        content={
             "message": "Duplicate Job Description found.",
            "existing_jd_id": str(exc.existing_jd.existing_jd.id),
            "title": exc.existing_jd.existing_jd.title,
            "version_number": exc.existing_jd.existing_jd.version_number,
        },
    )