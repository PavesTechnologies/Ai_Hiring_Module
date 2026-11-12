"""
Scoring versions (retire v1, phase D): shadow mode as "the current scoring version vs a candidate one".

- The current version scores every campaign: its configuration (the SCORING_V2_CONFIG shape, merged over
  the defaults) and, when set, its core-confirmation model. Its results are acted on.
- A campaign that tries a candidate version (hiring_campaigns.candidate_scoring_version_id) has each
  candidate scored with it too - stored, never acted on - for the shadow report.
- Promoting a candidate version makes it current (the previous current is retired) - HR_ADMIN, audited.
  Campaigns that were trying it stop shadowing: it now scores them for real.

A version differs only in configuration. Code changes (the confirmation prompt included - its version
is a hash of the prompt text) are proved by the golden set before merge, since two code versions can't
run side by side.
"""
import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.scoring_v2 import ScoringVersion
from app.services.campaign.scoring_v2 import DEFAULT_CONFIG, merge_config

logger = logging.getLogger(__name__)

STATUS_CURRENT, STATUS_CANDIDATE, STATUS_RETIRED = "current", "candidate", "retired"


def current_version(db) -> ScoringVersion | None:
    """The current scoring version; None before one exists (a fresh database before its seed)."""
    row = db.execute(select(ScoringVersion).where(ScoringVersion.status == STATUS_CURRENT)).scalars().first()
    return row if isinstance(row, ScoringVersion) else None


def version_config(version: ScoringVersion) -> dict:
    configuration = getattr(version, "configuration", None)
    return merge_config(configuration if isinstance(configuration, dict) else None)


def provider_resolver_for(db, version: ScoringVersion | None, default_resolver):
    """
    The core-confirmation provider for a version: its own model on the active provider (no backup, so
    every vote is that model's), or default_resolver when the version names no model.
    """
    if version is None or not version.model or default_resolver is None:
        return default_resolver
    model = version.model

    def resolve():
        from app.repositories.ai_provider_config_repository import AIProviderConfigRepository
        from app.services.llm.factory import resolve_provider_model
        active = AIProviderConfigRepository(db).get_active()
        return resolve_provider_model(db, active.provider if active else "GOOGLE", model)
    return resolve


# Evidence detection is shared by every version (one set of evidence rows per resume): only the
# current version's rules apply, so a candidate version can't carry its own.
SHARED_KEYS = ("evidence",)


def validate_configuration(configuration) -> dict:
    if not isinstance(configuration, dict):
        raise CampaignException("A scoring version's configuration must be a JSON object.", 422)
    unknown = sorted(set(configuration) - set(DEFAULT_CONFIG))
    if unknown:
        raise CampaignException(f"Unknown scoring configuration keys: {', '.join(unknown)}.", 422,
                                data={"unknown_keys": unknown, "known_keys": sorted(DEFAULT_CONFIG)})
    shared = sorted(set(configuration) & set(SHARED_KEYS))
    if shared:
        raise CampaignException(
            f"{', '.join(shared)} can't differ between scoring versions: evidence detection is shared by all "
            "of them (change it in code, proved by the golden set).", 422, data={"shared_keys": shared})
    # Settings phase 3: every nested value checked against its type and range.
    from app.services.campaign.scoring_config_schema import configuration_problems
    problems = configuration_problems(configuration)
    if problems:
        raise CampaignException("The scoring configuration has invalid values.", 422, data={"problems": problems})
    return configuration


def version_summary(version: ScoringVersion, campaigns: list[dict] | None = None) -> dict:
    return {
        "id": str(version.id), "label": version.label, "status": version.status, "model": version.model,
        "configuration": version.configuration or {}, "note": version.note,
        # Settings phase 3: every setting the version scores with (defaults filled in; evidence is shared).
        "effective_configuration": {k: v for k, v in version_config(version).items() if k not in SHARED_KEYS},
        "created_at": version.created_at, "activated_at": version.activated_at, "retired_at": version.retired_at,
        "campaigns_trying": campaigns or [],
    }


class ScoringVersionService:
    def __init__(self, db, audit_service):
        self.db = db
        self.audit_service = audit_service

    def _get(self, version_id: UUID) -> ScoringVersion:
        version = self.db.get(ScoringVersion, version_id)
        if version is None:
            raise CampaignException("Scoring version not found.", 404)
        return version

    def _audit(self, version: ScoringVersion, change: str, actor_id, actor_role, **details):
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.PLATFORM_CONFIG_UPDATED.value,
            entity_type=EntityType.PLATFORM_CONFIG.value, entity_id=version.id,
            details={"title": f"Scoring version {change}", "scoring_version_id": str(version.id),
                     "scoring_version_label": version.label, "change": change, **details},
        )

    def list_versions(self) -> list[dict]:
        from app.models.campaigns import HiringCampaign
        trying: dict = {}
        for campaign_id, name, version_id in self.db.execute(
                select(HiringCampaign.id, HiringCampaign.name, HiringCampaign.candidate_scoring_version_id)
                .where(HiringCampaign.candidate_scoring_version_id.isnot(None))).all():
            trying.setdefault(version_id, []).append({"campaign_id": str(campaign_id), "campaign": name})
        order = {STATUS_CURRENT: 0, STATUS_CANDIDATE: 1, STATUS_RETIRED: 2}
        versions = self.db.execute(select(ScoringVersion)).scalars().all()
        versions.sort(key=lambda v: (order.get(v.status, 3), -(v.created_at.timestamp() if v.created_at else 0)))
        return [version_summary(v, trying.get(v.id)) for v in versions]

    def create(self, *, label: str, configuration: dict, model: str | None, note: str | None,
               actor_id: str, actor_role: str | None) -> dict:
        label = (label or "").strip()
        if not label:
            raise CampaignException("A scoring version needs a label.", 422)
        if self.db.execute(select(ScoringVersion.id).where(ScoringVersion.label == label)).first():
            raise CampaignException(f"A scoring version labelled '{label}' already exists.", 409)
        model = (model or "").strip() or None
        version = ScoringVersion(label=label, configuration=validate_configuration(configuration), model=model,
                                 status=STATUS_CANDIDATE, note=(note or "").strip() or None, created_by=actor_id)
        self.db.add(version)
        self.db.flush()
        self._audit(version, "created", actor_id, actor_role, model=model,
                    configuration_keys=sorted(version.configuration))
        self.db.commit()
        return version_summary(version)

    def set_campaign_candidate(self, campaign_id: UUID, *, version_id: UUID | None, backfill: bool,
                               actor_id: str, actor_role: str | None) -> dict:
        """Try a candidate version on a campaign (None stops); backfill scores its scored candidates now."""
        from app.repositories.CampaignRepository import CampaignRepository
        campaign = CampaignRepository(self.db).get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        version = None
        if version_id is not None:
            version = self._get(version_id)
            if version.status != STATUS_CANDIDATE:
                raise CampaignException(f"Only a candidate version can be tried (this one is {version.status}).", 409)
        previous = campaign.candidate_scoring_version_id
        campaign.candidate_scoring_version_id = version.id if version else None
        self.audit_service.log(
            actor_id=actor_id, actor_role=actor_role, action_type=ActionType.CAMPAIGN_UPDATED.value,
            entity_type=EntityType.CAMPAIGN.value, entity_id=campaign_id, campaign_id=campaign_id,
            details={"candidate_scoring_version_id": str(version.id) if version else None,
                     "candidate_scoring_version_label": version.label if version else None,
                     "previous_candidate_scoring_version_id": str(previous) if previous else None,
                     "backfill": bool(backfill and version)},
        )
        self.db.commit()
        queued = self.backfill(campaign_id) if version is not None and backfill else 0
        return {"campaign_id": str(campaign_id),
                "candidate_scoring_version": version_summary(version) if version else None, "queued": queued}

    def backfill(self, campaign_id: UUID) -> int:
        """Queues the candidate-version score for the campaign's candidates already scored by the current one."""
        from app.models.candidates import ParseStatus, Resume
        from app.models.pipeline import CampaignCandidate
        from app.repositories.celery_task_log_repository import CeleryTaskLogRepository
        from app.services.celery_task_log_service import CeleryTaskLogService
        from app.tasks.scoring_v2_tasks import enqueue_scoring_v2
        log_service = CeleryTaskLogService(CeleryTaskLogRepository(self.db))
        candidates = self.db.execute(
            select(CampaignCandidate).join(Resume, Resume.id == CampaignCandidate.resume_id)
            .where(CampaignCandidate.campaign_id == campaign_id, Resume.parse_status == ParseStatus.PARSED,
                   CampaignCandidate.deterministic_score.isnot(None))
        ).scalars().all()
        queued = sum(int(bool(enqueue_scoring_v2(cc, log_service))) for cc in candidates)
        self.db.commit()
        return queued

    def promote(self, version_id: UUID, *, actor_id: str, actor_role: str | None) -> dict:
        """The candidate version becomes current; the current one is retired; campaigns trying it stop shadowing."""
        from app.models.campaigns import HiringCampaign
        version = self._get(version_id)
        if version.status != STATUS_CANDIDATE:
            raise CampaignException(f"Only a candidate version can be promoted (this one is {version.status}).", 409)
        # Settings phase 3: a PASSED golden run for exactly this configuration and model is required.
        from app.services.settings.golden_runs import GoldenRunService
        golden = GoldenRunService(self.db).passing_run(version)
        if golden is None:
            raise CampaignException(
                "Run the golden set for this version first: promotion needs a passing run for its exact "
                "configuration and model.", 422, data={"code": "GOLDEN_RUN_REQUIRED"})
        now = datetime.now(timezone.utc)
        previous = current_version(self.db)
        if previous is not None:
            previous.status, previous.retired_at = STATUS_RETIRED, now
            self.db.flush()                     # the one-current index: retire before activating
        version.status, version.activated_at, version.activated_by = STATUS_CURRENT, now, actor_id
        trying = self.db.execute(select(HiringCampaign).where(
            HiringCampaign.candidate_scoring_version_id == version.id)).scalars().all()
        for campaign in trying:
            campaign.candidate_scoring_version_id = None
        self._audit(version, "promoted", actor_id, actor_role,
                    previous_current_id=str(previous.id) if previous else None,
                    previous_current_label=previous.label if previous else None,
                    campaigns_stopped_trying=len(trying), golden_run_id=str(golden.id),
                    golden_llm_calls=golden.llm_calls)
        self.db.commit()
        logger.info("Scoring version promoted | version=%s previous=%s", version.label,
                    previous.label if previous else None)
        return version_summary(version)

    def retire(self, version_id: UUID, *, actor_id: str, actor_role: str | None) -> dict:
        """Retire a candidate version (campaigns trying it stop). The current one is replaced by promoting."""
        from app.models.campaigns import HiringCampaign
        version = self._get(version_id)
        if version.status != STATUS_CANDIDATE:
            raise CampaignException("Only a candidate version can be retired; promote another to replace the "
                                    "current one.", 409)
        version.status, version.retired_at = STATUS_RETIRED, datetime.now(timezone.utc)
        trying = self.db.execute(select(HiringCampaign).where(
            HiringCampaign.candidate_scoring_version_id == version.id)).scalars().all()
        for campaign in trying:
            campaign.candidate_scoring_version_id = None
        self._audit(version, "retired", actor_id, actor_role, campaigns_stopped_trying=len(trying))
        self.db.commit()
        return version_summary(version)
