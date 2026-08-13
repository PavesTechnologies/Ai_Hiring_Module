import logging
from datetime import datetime, time as dt_time, timedelta, timezone
from uuid import UUID

from app.enums.constants import ActionType, EntityType
from app.exceptions.campaign_exceptions import CampaignException
from app.models.campaigns import CampaignStatus

logger = logging.getLogger(__name__)

FREQUENCIES = ("DAILY", "WEEKLY", "BIWEEKLY")
FORMATS = ("XLSX", "PDF")
DAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]


class ScheduledExportService:
    """
    Recurring ranked-list exports per campaign.

    The config lives in hiring_campaigns.scheduled_export_config; there is no
    separate schedule table because a campaign has exactly one schedule and it
    is always read as a whole.

    Delivery is intentionally NOT performed here. Email is a separate module in
    this project, so this service decides *what* should be sent and *when*, and
    hands a fully-resolved payload to whatever dispatches it. That keeps the
    scheduling logic testable without a mail server.
    """

    def __init__(self, campaign_repo, export_service, audit_service, task_log_service=None):
        self.campaign_repo = campaign_repo
        self.export_service = export_service
        self.audit_service = audit_service
        self.task_log_service = task_log_service

    # ── configure ─────────────────────────────────────────────────────

    @staticmethod
    def _validate(cfg: dict) -> dict:
        freq = (cfg.get("frequency") or "").upper()
        if freq not in FREQUENCIES:
            raise CampaignException(
                f"frequency must be one of {', '.join(FREQUENCIES)}.", 422,
            )

        fmt = (cfg.get("format") or "XLSX").upper()
        if fmt not in FORMATS:
            raise CampaignException(f"format must be one of {', '.join(FORMATS)}.", 422)

        day = (cfg.get("day_of_week") or "").upper() or None
        if freq in ("WEEKLY", "BIWEEKLY"):
            if day not in DAYS:
                raise CampaignException(
                    f"{freq} schedules need day_of_week to be one of {', '.join(DAYS)}.", 422,
                )
        else:
            # A daily schedule with a day_of_week would silently contradict
            # itself, so it is dropped rather than stored and ignored.
            day = None

        # Explicit None check, not `or 10`: top_n=0 is falsy, so the shorter
        # form would silently rewrite an invalid 0 into a valid 10 instead of
        # rejecting it.
        raw_top_n = cfg.get("top_n")
        try:
            top_n = 10 if raw_top_n is None else int(raw_top_n)
        except (TypeError, ValueError):
            raise CampaignException("top_n must be a whole number between 1 and 50.", 422)
        if not 1 <= top_n <= 50:
            raise CampaignException("top_n must be between 1 and 50.", 422)

        raw_time = str(cfg.get("time") or "09:00")
        try:
            hh, mm = raw_time.split(":")[:2]
            parsed = dt_time(int(hh), int(mm))
        except Exception:
            raise CampaignException("time must be in HH:MM (24-hour) form.", 422)

        recipients = [r.strip() for r in (cfg.get("recipients") or []) if r and r.strip()]
        if not recipients:
            raise CampaignException("At least one recipient is required.", 422)
        for r in recipients:
            if "@" not in r or r.startswith("@") or r.endswith("@"):
                raise CampaignException(f"'{r}' is not a valid email address.", 422)

        return {
            "enabled": bool(cfg.get("enabled", True)),
            "paused": bool(cfg.get("paused", False)),
            "frequency": freq,
            "day_of_week": day,
            "time": f"{parsed.hour:02d}:{parsed.minute:02d}",
            "top_n": top_n,
            "format": fmt,
            "recipients": recipients,
        }

    def configure(self, campaign_id: UUID, cfg: dict, actor_id: str, actor_role: str | None) -> dict:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)

        validated = self._validate(cfg)
        existing = campaign.scheduled_export_config or {}
        # Never lose the delivery history when the schedule is edited.
        validated["last_sent_at"] = existing.get("last_sent_at")

        try:
            campaign.scheduled_export_config = validated
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.SCHEDULED_EXPORT_CONFIGURED.value,
                entity_type=EntityType.CAMPAIGN.value,
                entity_id=campaign_id,
                campaign_id=campaign_id,
                details={"title": "Scheduled export configured", **validated},
            )
            self.campaign_repo.commit()
        except Exception:
            self.campaign_repo.rollback()
            raise

        return self.describe(campaign_id)

    # ── pause / resume ────────────────────────────────────────────────

    def set_paused(self, campaign_id: UUID, paused: bool, actor_id: str, actor_role: str | None) -> dict:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        cfg = campaign.scheduled_export_config or {}
        if not cfg:
            raise CampaignException("This campaign has no scheduled export to pause.", 409)

        try:
            # Reassigned rather than mutated in place: SQLAlchemy does not track
            # in-place changes to a JSONB dict, so mutating it would not persist.
            campaign.scheduled_export_config = {**cfg, "paused": bool(paused)}
            self.audit_service.log(
                actor_id=actor_id,
                actor_role=actor_role,
                action_type=ActionType.SCHEDULED_EXPORT_CONFIGURED.value,
                entity_type=EntityType.CAMPAIGN.value,
                entity_id=campaign_id,
                campaign_id=campaign_id,
                details={"title": f"Scheduled export {'paused' if paused else 'resumed'}"},
            )
            self.campaign_repo.commit()
        except Exception:
            self.campaign_repo.rollback()
            raise
        return self.describe(campaign_id)

    def disable(self, campaign_id: UUID, actor_id: str, actor_role: str | None) -> dict:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        try:
            campaign.scheduled_export_config = None
            self.audit_service.log(
                actor_id=actor_id, actor_role=actor_role,
                action_type=ActionType.SCHEDULED_EXPORT_CONFIGURED.value,
                entity_type=EntityType.CAMPAIGN.value, entity_id=campaign_id,
                campaign_id=campaign_id,
                details={"title": "Scheduled export disabled"},
            )
            self.campaign_repo.commit()
        except Exception:
            self.campaign_repo.rollback()
            raise
        return {"campaign_id": campaign_id, "configured": False}

    # ── scheduling maths ──────────────────────────────────────────────

    @staticmethod
    def next_run_at(cfg: dict, *, now: datetime | None = None) -> datetime | None:
        """
        Next fire time in UTC, or None when the schedule cannot fire.

        Pure and now-injectable so the beat task's behaviour is testable
        without waiting for a clock.
        """
        if not cfg or not cfg.get("enabled") or cfg.get("paused"):
            return None

        now = now or datetime.now(timezone.utc)
        hh, mm = (int(x) for x in str(cfg.get("time", "09:00")).split(":")[:2])
        candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

        freq = (cfg.get("frequency") or "DAILY").upper()
        if freq == "DAILY":
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate

        target = cfg.get("day_of_week")
        if target not in DAYS:
            return None
        target_idx = DAYS.index(target)

        delta = (target_idx - candidate.weekday()) % 7
        candidate += timedelta(days=delta)
        if candidate <= now:
            candidate += timedelta(days=7)

        if freq == "BIWEEKLY":
            last = cfg.get("last_sent_at")
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    # Only skip forward when the computed slot is inside the
                    # fortnight already served by the last delivery.
                    if (candidate - last_dt).days < 14:
                        candidate += timedelta(days=7)
                except ValueError:
                    logger.warning("Unparseable last_sent_at in schedule: %r", last)
        return candidate

    def is_due(self, campaign, *, now: datetime | None = None) -> bool:
        """
        T02: a campaign that is not ACTIVE never fires, regardless of its
        schedule — checked here rather than only at send time so a paused or
        closed campaign cannot leak an export through any caller.
        """
        cfg = campaign.scheduled_export_config or {}
        if not cfg.get("enabled") or cfg.get("paused"):
            return False
        if campaign.status != CampaignStatus.ACTIVE:
            return False

        now = now or datetime.now(timezone.utc)
        hh, mm = (int(x) for x in str(cfg.get("time", "09:00")).split(":")[:2])
        slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < slot:
            return False

        freq = (cfg.get("frequency") or "DAILY").upper()
        if freq in ("WEEKLY", "BIWEEKLY"):
            target = cfg.get("day_of_week")
            if target not in DAYS or DAYS.index(target) != now.weekday():
                return False

        last = cfg.get("last_sent_at")
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                return True
            min_gap = {"DAILY": 1, "WEEKLY": 7, "BIWEEKLY": 14}[freq]
            # Guards against a double send if beat runs twice in one slot.
            if (now - last_dt) < timedelta(hours=23) or (now - last_dt).days < min_gap - 1:
                return False
        return True

    def mark_sent(self, campaign, when: datetime | None = None) -> None:
        cfg = campaign.scheduled_export_config or {}
        if not cfg:
            return
        when = when or datetime.now(timezone.utc)
        try:
            campaign.scheduled_export_config = {**cfg, "last_sent_at": when.isoformat()}
            self.campaign_repo.commit()
        except Exception:
            self.campaign_repo.rollback()
            raise

    # ── read model ────────────────────────────────────────────────────

    def describe(self, campaign_id: UUID) -> dict:
        campaign = self.campaign_repo.get_by_id(campaign_id)
        if campaign is None:
            raise CampaignException("Campaign not found.", 404)
        cfg = campaign.scheduled_export_config or {}
        if not cfg:
            return {"campaign_id": campaign_id, "configured": False}

        nxt = self.next_run_at(cfg)
        # A schedule on a non-ACTIVE campaign is auto-suspended, and says so
        # rather than silently showing a next-run time that will never fire.
        suspended = campaign.status != CampaignStatus.ACTIVE
        return {
            "campaign_id": campaign_id,
            "configured": True,
            "enabled": cfg.get("enabled", False),
            "paused": cfg.get("paused", False),
            "auto_suspended": suspended,
            "frequency": cfg.get("frequency"),
            "day_of_week": cfg.get("day_of_week"),
            "time": cfg.get("time"),
            "top_n": cfg.get("top_n"),
            "format": cfg.get("format"),
            "recipients": cfg.get("recipients", []),
            "last_sent_at": cfg.get("last_sent_at"),
            "next_run_at": None if suspended else (nxt.isoformat() if nxt else None),
        }

    # ── history ───────────────────────────────────────────────────────

    def history(self, campaign_id: UUID, limit: int = 20) -> list[dict]:
        """
        Read-only delivery history from celery_task_log. Returns [] rather than
        raising when the log is unavailable — an empty history panel is a far
        better failure than a broken campaign page.
        """
        try:
            rows = self.export_service.export_repo.export_history(campaign_id, limit=limit)
        except Exception:
            logger.exception("Scheduled export history unavailable for %s", campaign_id)
            return []

        out = []
        for r in rows:
            status = r.status.value if hasattr(r.status, "value") else str(r.status)
            out.append({
                "task_id": r.task_id,
                "title": r.title,
                "generated_at": r.completed_at or r.queued_at,
                "status": status,
                # A scheduled run carries SCHEDULED in its idempotency key; a
                # manual large export does not. That is what distinguishes them.
                "scheduled": ":SCHEDULED:" in (r.idempotency_key or ""),
                "duration_ms": r.duration_ms,
                "download_url": r.output_summary,
                "error": r.error_message,
                "failed": status in ("FAILURE", "DEAD"),
            })
        return out
