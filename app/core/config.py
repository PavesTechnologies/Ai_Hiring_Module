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
    db_pool_size: int = 1
    db_max_overflow: int = 2

    # Redis
    redis_host: str = "localhost"
    redis_port: str = "6379"
    redis_username: str = ""
    redis_password: str = ""
    redis_db: int = 3

    # AWS S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "ap-south-1"
    aws_s3_bucket: str = ""

    # AWS SES (M07-E03 S02 T02) — reuses the same AWS credentials/region
    # above; only the verified sender address is SES-specific.
    ses_from_email: str = ""

    # Supabase
    SUPABASE_URL:str
    SUPABASE_PUBLISHABLE_KEY:str
    SUPABASE_SECRET_KEY:str
    SUPABASE_JWKS_URL:str

    # AI / Embeddings
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-latest"
    embedding_model: str = "all-MiniLM-L6-v2"

    # Encryption
    candidate_pii_key: str = ""

    # Microsoft Teams calendar integration (M12) — delegated OAuth,
    # Calendars.ReadWrite/OnlineMeetings.ReadWrite/offline_access/User.Read.
    # No admin-consent gate; each user goes through /oauth/microsoft/connect
    # individually.
    microsoft_client_id: str = ""
    microsoft_tenant_id: str = ""
    microsoft_client_secret: str = ""
    microsoft_redirect_uri: str = ""

    # Google Meet calendar integration (M12) - same delegated-OAuth shape
    # as Microsoft above, calendar.events scope only.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""

    # HMAC-signs the OAuth `state` param so /oauth/microsoft/callback (which
    # never carries our own Authorization header - see JWTMiddleware's
    # public-path bypass for that route) can still verify which user
    # initiated the connect flow, without a session store this codebase
    # doesn't otherwise have.
    oauth_state_signing_key: str = ""

    # UMS — User Management System (token issuer)
    ums_url: str   # required — set UMS_URL in .env

    # CORS — list explicit origins; credentials require non-wildcard origins
    cors_origins: Annotated[list[str], NoDecode]

    # App
    app_env: str = "development"
    debug: bool = True

    # Frontend base URL - used to build direct links (e.g. a campaign
    # monitoring link) in platform alert emails. Empty by default so a
    # missing FRONTEND_URL env var never crashes anything that builds a
    # link from it - it just produces a relative path instead of an
    # absolute URL.
    frontend_base_url: str = ""


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

    @property
    def redis_url(self) -> str:
        auth = ""
        if self.redis_username or self.redis_password:
            auth = f"{self.redis_username}:{self.redis_password}@"
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def CELERY_BROKER_URL(self) -> str:
        return self.redis_url

    @property
    def CELERY_RESULT_BACKEND(self) -> str:
        return self.redis_url

    model_config = {"env_file": ".env", "case_sensitive": False,"extra": "ignore"}


settings = Settings()
