from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch
from uuid import uuid4

from app.services.candidate_change_notifier import CandidateChangeNotifier

PUBLISHER = "app.services.candidate_change_notifier.publisher"


def _cc():
    return SimpleNamespace(id=uuid4(), candidate_id=uuid4(), resume_id=uuid4())


def test_stage_changed_invalidates_before_publishing():
    order = []
    invalidator = MagicMock()
    invalidator.campaign_candidates.side_effect = lambda *a: order.append("invalidate")
    campaign_id, cc = uuid4(), _cc()

    with patch(PUBLISHER) as publisher:
        publisher.publish_board_stage_changed.side_effect = lambda *a: order.append("publish")
        CandidateChangeNotifier(invalidator).stage_changed(campaign_id, cc)

    invalidator.campaign_candidates.assert_called_once_with(campaign_id, [cc.resume_id])
    publisher.publish_board_stage_changed.assert_called_once_with(campaign_id, cc)
    assert order == ["invalidate", "publish"]


def test_stage_changed_many_invalidates_once_and_publishes_per_candidate():
    invalidator = MagicMock()
    campaign_id, a, b = uuid4(), _cc(), _cc()

    with patch(PUBLISHER) as publisher:
        CandidateChangeNotifier(invalidator).stage_changed_many(campaign_id, [a, b])

    invalidator.campaign_candidates.assert_called_once_with(campaign_id, [a.resume_id, b.resume_id])
    assert publisher.publish_board_stage_changed.call_args_list == [call(campaign_id, a), call(campaign_id, b)]


def test_updated_and_removed_route_to_their_events():
    invalidator = MagicMock()
    campaign_id, cc_id, resume_id = uuid4(), uuid4(), uuid4()

    with patch(PUBLISHER) as publisher:
        notifier = CandidateChangeNotifier(invalidator)
        notifier.updated(campaign_id, cc_id, resume_id)
        notifier.removed(campaign_id, cc_id)

    publisher.publish_board_candidate_updated.assert_called_once_with(campaign_id, cc_id)
    publisher.publish_board_candidate_removed.assert_called_once_with(campaign_id, cc_id)
    assert invalidator.campaign_candidates.call_args_list == [
        call(campaign_id, [resume_id]), call(campaign_id, []),
    ]


def test_failures_never_propagate_and_publish_still_runs():
    invalidator = MagicMock()
    invalidator.campaign_candidates.side_effect = Exception("redis down")

    with patch(PUBLISHER) as publisher:
        publisher.publish_board_candidate_added.side_effect = Exception("pubsub down")
        CandidateChangeNotifier(invalidator).added(uuid4(), _cc())  # must not raise

    publisher.publish_board_candidate_added.assert_called_once()
