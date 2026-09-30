import logging
from typing import Any, Iterable

from app.core.cache_invalidation import CacheInvalidator
from app.websocket import publisher

logger = logging.getLogger(__name__)


class CandidateChangeNotifier:
    """
    What every campaign-candidate change must do once its DB write has
    committed: drop each cached view that shows the candidate (campaign
    counts/flags, resume and candidate lists), then tell live campaign
    boards. Must be called after commit - a board client refetches on the
    event, so it has to read committed data. Never raises: the change itself
    already succeeded.
    """

    def __init__(self, invalidator: CacheInvalidator | None = None):
        self.invalidator = invalidator or CacheInvalidator.default()

    def added(self, campaign_id: Any, campaign_candidate) -> None:
        self._invalidate(campaign_id, [getattr(campaign_candidate, "resume_id", None)])
        self._publish(publisher.publish_board_candidate_added, campaign_id, campaign_candidate)

    def stage_changed(self, campaign_id: Any, campaign_candidate) -> None:
        self.stage_changed_many(campaign_id, [campaign_candidate])

    def stage_changed_many(self, campaign_id: Any, campaign_candidates: Iterable) -> None:
        """One invalidation for the whole batch, then one board event per candidate."""
        campaign_candidates = list(campaign_candidates)
        self._invalidate(campaign_id, [getattr(cc, "resume_id", None) for cc in campaign_candidates])
        for campaign_candidate in campaign_candidates:
            self._publish(publisher.publish_board_stage_changed, campaign_id, campaign_candidate)

    def updated(self, campaign_id: Any, campaign_candidate_id: Any, resume_id: Any = None) -> None:
        self._invalidate(campaign_id, [resume_id])
        self._publish(publisher.publish_board_candidate_updated, campaign_id, campaign_candidate_id)

    def removed(self, campaign_id: Any, campaign_candidate_id: Any, resume_ids: Iterable[Any] = ()) -> None:
        self._invalidate(campaign_id, resume_ids)
        self._publish(publisher.publish_board_candidate_removed, campaign_id, campaign_candidate_id)

    def _invalidate(self, campaign_id: Any, resume_ids: Iterable[Any]) -> None:
        try:
            self.invalidator.campaign_candidates(campaign_id, [rid for rid in resume_ids if rid is not None])
        except Exception:
            logger.exception("Cache invalidation failed for campaign_id=%s", campaign_id)

    @staticmethod
    def _publish(publish_fn, campaign_id: Any, subject) -> None:
        try:
            publish_fn(campaign_id, subject)
        except Exception:
            logger.exception(
                "Board event %s failed for campaign_id=%s", getattr(publish_fn, "__name__", publish_fn), campaign_id,
            )
