import json
from datetime import datetime, timezone
from uuid import UUID

from app.models.compliance import CandidateConsent
from app.repositories.config_repository import ConfigRepository
from app.repositories.consent_repository import ConsentRepository

DEFAULT_CONSENT_VERSION = "1.0"


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


class ConsentService:
    def __init__(self, consent_repo: ConsentRepository, config_repo: ConfigRepository):
        self.consent_repo = consent_repo
        self.config_repo = config_repo

    def record_consent(
        self,
        candidate_id: UUID,
        jurisdiction: str,
        source: str,
        consent_given: bool = True,
        consent_version: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> CandidateConsent:
        """
        Inserts a new candidate_consent row. Does NOT commit — the caller
        (e.g. CandidateService in Phase 3) is expected to commit this in the
        same transaction as the candidate record it belongs to.
        """
        consent = CandidateConsent(
            candidate_id=candidate_id,
            consent_given=consent_given,
            consent_version=consent_version or self._default_consent_version(),
            jurisdiction=jurisdiction,
            consent_timestamp=datetime.now(timezone.utc),
            consent_source=source,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return self.consent_repo.create(consent)

    def _default_consent_version(self) -> str:
        configs = self.config_repo.get_configs_by_keys(["CONSENT_VERSION"])
        return configs.get("CONSENT_VERSION", DEFAULT_CONSENT_VERSION)

    def _jurisdiction_config(self, jurisdiction: str) -> dict:
        configs = self.config_repo.get_configs_by_keys(["JURISDICTION_CONSENT_CONFIG"])
        raw = configs.get("JURISDICTION_CONSENT_CONFIG")
        if not raw:
            return {}

        try:
            all_jurisdictions = json.loads(raw)
        except json.JSONDecodeError:
            return {}

        return all_jurisdictions.get(jurisdiction) or all_jurisdictions.get("GLOBAL", {})

