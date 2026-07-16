import json
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode


class Settings(BaseSettings):
    # Database (Aiven PostgreSQL)
    db_user: str
    db_password: str
    db_host: str
    db_port: str = "5432"
    db_name: str
    db_driver: str = "postgresql+psycopg2"
    db_sslmode: str = "require"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # AWS S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "ap-south-1"
    aws_s3_bucket: str = ""

    # Supabase
    SUPABASE_URL:str
    SUPABASE_PUBLISHABLE_KEY:str
    SUPABASE_SECRET_KEY:str
    SUPABASE_JWKS_URL:str

    # AI / Embeddings
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    embedding_model: str = "all-MiniLM-L6-v2"

    # Encryption
    candidate_pii_key: str = ""

    # UMS — User Management System (token issuer)
    ums_url: str   # required — set UMS_URL in .env

    # CORS — list explicit origins; credentials require non-wildcard origins
    cors_origins: Annotated[list[str], NoDecode]

    # App
    app_env: str = "development"
    debug: bool = True


    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, list):
            return value

        value = value.strip()
        if not value:
            return []

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            parsed = None

        if isinstance(parsed, list):
            return [str(origin).strip() for origin in parsed if str(origin).strip()]

        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]

        return [origin.strip().strip('"').strip("'") for origin in value.split(",") if origin.strip()]

    @field_validator("debug", mode="before")
    @classmethod
    def parse_debug(cls, value: str | bool) -> bool | str:
        if isinstance(value, bool):
            return value

        value = value.strip().lower()
        if value in {"release", "production", "prod"}:
            return False
        if value in {"development", "dev", "local"}:
            return True
        return value

    @property
    def database_url(self) -> str:
        return (
            f"{self.db_driver}://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
            f"?sslmode={self.db_sslmode}"
        )

    model_config = {"env_file": ".env", "case_sensitive": False,"extra": "ignore"}


settings = Settings()
