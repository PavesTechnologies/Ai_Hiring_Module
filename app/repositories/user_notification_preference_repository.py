from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.email import EmailTriggerEvent, UserNotificationPreference


class UserNotificationPreferenceRepository:

    def __init__(self, db: Session):
        self.db = db

    def get_all_by_user_id(self, user_id: str) -> list[UserNotificationPreference]:
        return (
            self.db.query(UserNotificationPreference)
            .filter(UserNotificationPreference.user_id == user_id)
            .all()
        )

    def get_by_user_id_and_trigger_event(
        self, user_id: str, trigger_event: EmailTriggerEvent,
    ) -> UserNotificationPreference | None:
        return (
            self.db.query(UserNotificationPreference)
            .filter(
                UserNotificationPreference.user_id == user_id,
                UserNotificationPreference.trigger_event == trigger_event,
            )
            .first()
        )

    def upsert(self, user_id: str, trigger_event: EmailTriggerEvent, is_enabled: bool) -> UserNotificationPreference:
        """
        SAVEPOINT + IntegrityError-catch on UNIQUE(user_id, trigger_event)
        - same shape as every other upsert-style write in this codebase
          (campaign_candidate_repository.create_idempotent,
          InterviewScheduleRepository.create_next_round,
          InterviewFeedbackRepository.create). A genuine race here means
          the same user submitting 2 concurrent PUTs for the same
          trigger_event - real, if unlikely, and this table's UNIQUE
          constraint makes it a possible IntegrityError, not just a
          theoretical one. Unlike InterviewFeedbackRepository's hard
          lock, this is a plain toggle: the loser's is_enabled value is
          still applied onto the winner's row (last write wins), not
          discarded - there's no append-only guarantee to protect here,
          just a current preference value.
        """
        existing = self.get_by_user_id_and_trigger_event(user_id, trigger_event)
        if existing is not None:
            existing.is_enabled = is_enabled
            self.db.flush()
            self.db.refresh(existing)
            return existing

        preference = UserNotificationPreference(user_id=user_id, trigger_event=trigger_event, is_enabled=is_enabled)
        try:
            with self.db.begin_nested():
                self.db.add(preference)
                self.db.flush()
        except IntegrityError:
            winner = self.get_by_user_id_and_trigger_event(user_id, trigger_event)
            winner.is_enabled = is_enabled
            self.db.flush()
            self.db.refresh(winner)
            return winner

        self.db.refresh(preference)
        return preference

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
